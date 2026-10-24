//! Desktop process boundary. The low-level library router is never served directly.
//! The launcher supplies a fresh 32-byte secret as 64 lowercase hex characters over
//! inherited stdin. No credential is accepted from a URL, environment or ready file.

use std::{hint::black_box, io::Read, net::IpAddr, sync::Arc};

use axum::{
    Json, Router,
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header, uri::Authority},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::get,
};
use open_karaoke_core::{AppState, CORE_API_VERSION, CORE_API_VERSION_HEADER, app};
use serde_json::json;
use tokio::sync::Semaphore;
use uuid::Uuid;

const MAX_ACTIVE_HANDLERS: usize = 64;

/// {
///   責務: [`CoreSession`: Coreセッションのtokenと識別子、同時処理枠を所有する]
///   フィールド: [
///     `token`: セッション秘密(64byte),
///     `instance_id`: Core起動ごとの識別子,
///     `request_budget`: 同時に処理できる要求数の枠
///   ]
///   補足: [秘密がログへ出ないよう、意図的にDebug/Serializeを実装しない]
/// }
#[derive(Clone)]
pub(crate) struct CoreSession {
    token: Arc<[u8; 64]>,
    instance_id: Uuid,
    request_budget: Arc<Semaphore>,
}

impl CoreSession {
    pub(crate) fn read_from(input: &mut impl Read) -> Result<Self, &'static str> {
        let mut token = [0_u8; 64];
        input
            .read_exact(&mut token)
            .map_err(|_| "Core session bootstrap is incomplete")?;
        if !token
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        {
            return Err("Core session token must be 64 lowercase hex characters");
        }
        let mut delimiter = [0_u8; 1];
        input
            .read_exact(&mut delimiter)
            .map_err(|_| "Core session bootstrap needs a newline")?;
        if delimiter[0] == b'\r' {
            input
                .read_exact(&mut delimiter)
                .map_err(|_| "Core session bootstrap needs a newline")?;
        }
        if delimiter[0] != b'\n' {
            return Err("Core session bootstrap exceeds its fixed size");
        }
        Ok(Self {
            token: Arc::new(token),
            instance_id: Uuid::new_v4(),
            request_budget: Arc::new(Semaphore::new(MAX_ACTIVE_HANDLERS)),
        })
    }

    pub(crate) fn instance_id(&self) -> Uuid {
        self.instance_id
    }

    pub(crate) fn router(self, state: AppState) -> Router {
        let instance_id = self.instance_id;
        app(state)
            .route(
                "/session",
                get(move || async move {
                    Json(json!({
                        "service": "open-karaoke-core",
                        "api_version": CORE_API_VERSION,
                        "instance_id": instance_id,
                    }))
                }),
            )
            // Apply last: all handlers, fallback responses and future routes inside
            // the product router require authorization before doing any work.
            .layer(middleware::from_fn_with_state(self, authorize))
    }

    fn accepts(&self, headers: &HeaderMap) -> bool {
        let mut values = headers.get_all(header::AUTHORIZATION).iter();
        let Some(value) = values.next() else {
            return false;
        };
        if values.next().is_some() {
            return false;
        }
        let Some(candidate) = value.as_bytes().strip_prefix(b"Bearer ") else {
            return false;
        };
        if candidate.len() != self.token.len() {
            return false;
        }
        // Fixed-size scan without a secret-dependent early return. black_box
        // prevents replacing this loop with an early-exit byte comparison.
        // This is not a claim of a hardware side-channel security audit.
        let mut different = 0_u8;
        for (actual, expected) in candidate.iter().zip(self.token.iter()) {
            different |= black_box(*actual ^ *expected);
        }
        black_box(different) == 0
    }
}

fn local_host(headers: &HeaderMap) -> bool {
    let mut values = headers.get_all(header::HOST).iter();
    let Some(value) = values.next() else {
        return false;
    };
    if values.next().is_some() {
        return false;
    }
    let Some(authority) = value
        .to_str()
        .ok()
        .and_then(|value| value.parse::<Authority>().ok())
    else {
        return false;
    };
    authority
        .host()
        .trim_start_matches('[')
        .trim_end_matches(']')
        .parse::<IpAddr>()
        .is_ok_and(|ip| ip.is_loopback())
}

fn rejected(status: StatusCode, message: &'static str) -> Response {
    let mut response = (status, Json(json!({"error": message}))).into_response();
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    if status == StatusCode::UNAUTHORIZED {
        response.headers_mut().insert(
            header::WWW_AUTHENTICATE,
            HeaderValue::from_static("Bearer realm=\"OpenKaraoke\""),
        );
    }
    response
}

/// {
///   責務: [`normalize_error_response`: framework由来の非JSONエラー応答を`{"error":"message"}`へ揃える]
///   処理: [
///     1: 成功応答、または既にJSONのエラー応答はそのまま返す,
///     2: 405の`Allow`ヘッダーを退避する,
///     3: 404・405・413に専用メッセージ、その他に汎用メッセージを選ぶ,
///     4: JSONエラー応答を作り、`Allow`を戻す
///   ]
///   引数: [`response`: 後続handlerの応答]
///   戻り値: [JSONエラーへ正規化した応答、または元の応答]
/// }
fn normalize_error_response(response: Response) -> Response {
    let status = response.status();
    if !(status.is_client_error() || status.is_server_error())
        || response
            .headers()
            .get(header::CONTENT_TYPE)
            .is_some_and(|value| value.as_bytes().starts_with(b"application/json"))
    {
        return response;
    }
    let allow = response.headers().get(header::ALLOW).cloned();
    let message = match status {
        StatusCode::NOT_FOUND => "Core route not found",
        StatusCode::METHOD_NOT_ALLOWED => "Core method not allowed",
        StatusCode::PAYLOAD_TOO_LARGE => "Core request body too large",
        _ => "Core request rejected",
    };
    let mut normalized = rejected(status, message);
    if let Some(allow) = allow {
        normalized.headers_mut().insert(header::ALLOW, allow);
    }
    normalized
}

/// {
///   責務: [`authorize`: 認証・Host・Origin・versionの検証と同時処理枠の確保を行うmiddleware]
///   処理: [
///     1: `/health` 以外でブラウザ由来・token・Host・versionを検証する,
///     2: 同時処理枠を確保できなければ429を返す,
///     3: 後続handlerを実行し、応答生成後に枠を返す,
///     4: 非JSONのエラー応答をJSONへ正規化する,
///     5: 応答へ `Cache-Control: no-store` を付ける
///   ]
///   引数: [`session`: 検証と処理枠を持つセッション, `request`: 受信要求, `next`: 後続handler]
///   戻り値: [検証失敗・上限超過・handler結果のいずれかの応答]
/// }
async fn authorize(State(session): State<CoreSession>, request: Request, next: Next) -> Response {
    let public_health = request.method() == Method::GET && request.uri().path() == "/health";
    if !public_health {
        let headers = request.headers();
        if headers.contains_key(header::ORIGIN) || headers.contains_key("sec-fetch-site") {
            return rejected(StatusCode::FORBIDDEN, "Browser access is not permitted");
        }
        if !session.accepts(headers) {
            return rejected(
                StatusCode::UNAUTHORIZED,
                "Core session authorization required",
            );
        }
        if !local_host(headers) {
            return rejected(StatusCode::BAD_REQUEST, "Numeric loopback Host required");
        }
        let mut versions = headers.get_all(CORE_API_VERSION_HEADER).iter();
        if versions
            .next()
            .is_some_and(|value| value.as_bytes() != b"1")
            || versions.next().is_some()
        {
            return rejected(StatusCode::UPGRADE_REQUIRED, "Unsupported Core API version");
        }
    }
    let Ok(permit) = Arc::clone(&session.request_budget).try_acquire_owned() else {
        return rejected(StatusCode::TOO_MANY_REQUESTS, "Too many Core requests");
    };
    let response = next.run(request).await;
    drop(permit);
    let mut response = normalize_error_response(response);
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

#[cfg(test)]
mod tests {
    use axum::{body::Body, http::Request};
    use tower::ServiceExt;

    use super::*;

    const TOKEN: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn session(token: &str) -> CoreSession {
        CoreSession::read_from(&mut format!("{token}\n").as_bytes()).unwrap()
    }

    #[test]
    fn bootstrap_is_bounded_and_never_echoes_invalid_secrets() {
        let first = session(TOKEN);
        let second = session(TOKEN);
        assert_ne!(first.instance_id(), second.instance_id());
        assert!(CoreSession::read_from(&mut format!("{TOKEN}\r\n").as_bytes()).is_ok());
        for input in [
            String::new(),
            format!("{}\n", "a".repeat(63)),
            format!("{}\n", "a".repeat(65)),
            format!("{}\n", "G".repeat(64)),
            TOKEN.to_owned(),
        ] {
            let error = CoreSession::read_from(&mut input.as_bytes()).err().unwrap();
            assert!(!error.contains(TOKEN));
        }
    }

    #[tokio::test]
    async fn every_product_route_requires_a_session() {
        let router = session(TOKEN).router(AppState::new());
        for (method, path) in [
            ("GET", "/models"),
            ("GET", "/devices"),
            ("GET", "/analysis/health"),
            ("GET", "/session"),
            ("POST", "/jobs"),
            ("GET", "/jobs/00000000-0000-0000-0000-000000000000"),
            ("POST", "/jobs/00000000-0000-0000-0000-000000000000/cancel"),
            ("GET", "/jobs/00000000-0000-0000-0000-000000000000/events"),
            ("GET", "/future-route"),
        ] {
            let response = router
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri(path)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{path}");
            assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
        }
        let response = router
            .oneshot(
                Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn token_host_origin_and_version_negative_cases() {
        let router = session(TOKEN).router(AppState::new());
        for (name, value, status) in [
            (
                "authorization",
                "Bearer wrong".to_owned(),
                StatusCode::UNAUTHORIZED,
            ),
            ("origin", "null".to_owned(), StatusCode::FORBIDDEN),
            (
                "sec-fetch-site",
                "same-origin".to_owned(),
                StatusCode::FORBIDDEN,
            ),
            (
                "host",
                "attacker.invalid:32145".to_owned(),
                StatusCode::BAD_REQUEST,
            ),
            (
                CORE_API_VERSION_HEADER,
                "2".to_owned(),
                StatusCode::UPGRADE_REQUIRED,
            ),
        ] {
            let mut request = Request::builder()
                .uri("/session")
                .header("host", "127.0.0.1:32145")
                .header("authorization", format!("Bearer {TOKEN}"))
                .body(Body::empty())
                .unwrap();
            request.headers_mut().insert(name, value.parse().unwrap());
            let response = router.clone().oneshot(request).await.unwrap();
            assert_eq!(response.status(), status, "{name}");
        }
    }

    #[tokio::test]
    async fn duplicate_or_query_credentials_and_previous_session_are_rejected() {
        let router = session(TOKEN).router(AppState::new());
        let mut duplicate = Request::builder()
            .uri("/session")
            .header("host", "127.0.0.1:32145")
            .header("authorization", format!("Bearer {TOKEN}"))
            .body(Body::empty())
            .unwrap();
        duplicate
            .headers_mut()
            .append("authorization", format!("Bearer {TOKEN}").parse().unwrap());
        assert_eq!(
            router.clone().oneshot(duplicate).await.unwrap().status(),
            StatusCode::UNAUTHORIZED
        );
        let query = Request::builder()
            .uri(format!("/session?token={TOKEN}"))
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            router.oneshot(query).await.unwrap().status(),
            StatusCode::UNAUTHORIZED
        );
        let replacement = session(&"f".repeat(64)).router(AppState::new());
        let stale = Request::builder()
            .uri("/session")
            .header("host", "127.0.0.1:32145")
            .header("authorization", format!("Bearer {TOKEN}"))
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            replacement.oneshot(stale).await.unwrap().status(),
            StatusCode::UNAUTHORIZED
        );
    }

    /// {
    ///   責務: [`request_budget_rejects_overflow_and_recovers`: 同時処理の上限と枠の返却を確認する]
    ///   処理: [
    ///     1: 処理枠を全て占有した状態で認証済み要求が429になることを確認する,
    ///     2: 429にもCache-Control: no-storeが付くことを確認する,
    ///     3: 枠を1つ返すと要求が受理され、応答後に枠が戻ることを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[tokio::test]
    async fn request_budget_rejects_overflow_and_recovers() {
        let session = session(TOKEN);
        let budget = Arc::clone(&session.request_budget);
        let router = session.router(AppState::new());
        let request = || {
            Request::builder()
                .uri("/session")
                .header("host", "127.0.0.1:32145")
                .header("authorization", format!("Bearer {TOKEN}"))
                .body(Body::empty())
                .unwrap()
        };

        let held = budget
            .clone()
            .try_acquire_many_owned(u32::try_from(MAX_ACTIVE_HANDLERS).unwrap())
            .unwrap();
        let rejected = router.clone().oneshot(request()).await.unwrap();
        assert_eq!(rejected.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(rejected.headers()[header::CACHE_CONTROL], "no-store");

        drop(held);
        let accepted = router.oneshot(request()).await.unwrap();
        assert_eq!(accepted.status(), StatusCode::OK);
        assert_eq!(budget.available_permits(), MAX_ACTIVE_HANDLERS);
    }

    /// {
    ///   責務: [`framework_errors_are_normalized_to_json`: framework由来のエラーがJSON契約に揃うことを確認する]
    ///   処理: [
    ///     1: 未知のパスが404のJSONエラーになることを確認する,
    ///     2: 許可されない操作が405のJSONエラーで`Allow`を保持することを確認する,
    ///     3: 上限超過の本文が413のJSONエラーになることを確認する,
    ///     4: 不正なJSON本文がJSONエラーになることを確認する
    ///   ]
    ///   引数: []
    ///   戻り値: []
    /// }
    #[tokio::test]
    async fn framework_errors_are_normalized_to_json() {
        let router = session(TOKEN).router(AppState::new());
        let send = |method: &'static str, path: &'static str, body: Vec<u8>| {
            let router = router.clone();
            async move {
                let response = router
                    .oneshot(
                        Request::builder()
                            .method(method)
                            .uri(path)
                            .header("host", "127.0.0.1:32145")
                            .header("authorization", format!("Bearer {TOKEN}"))
                            .header(CORE_API_VERSION_HEADER, "1")
                            .header("content-type", "application/json")
                            .body(Body::from(body))
                            .unwrap(),
                    )
                    .await
                    .unwrap();
                let status = response.status();
                let headers = response.headers().clone();
                let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
                    .await
                    .unwrap();
                (
                    status,
                    headers,
                    serde_json::from_slice::<serde_json::Value>(&bytes),
                )
            }
        };

        let (status, headers, body) = send("GET", "/no-such-route", Vec::new()).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(body.unwrap()["error"], "Core route not found");
        assert_eq!(headers[header::CACHE_CONTROL], "no-store");

        let (status, headers, body) = send("DELETE", "/session", Vec::new()).await;
        assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
        assert_eq!(body.unwrap()["error"], "Core method not allowed");
        assert!(headers.contains_key(header::ALLOW));

        let (status, _, body) = send("POST", "/jobs", vec![b' '; 65 * 1024]).await;
        assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(body.unwrap()["error"], "Core request body too large");

        let (status, _, body) = send("POST", "/jobs", b"{not json".to_vec()).await;
        assert!(status.is_client_error());
        assert!(body.unwrap()["error"].is_string());
    }

    #[tokio::test]
    async fn native_client_can_read_identity_and_create_jobs() {
        let router = session(TOKEN).router(AppState::new());
        for (method, path, body, expected) in [
            ("GET", "/session", "", StatusCode::OK),
            (
                "POST",
                "/jobs",
                r#"{"kind":"test","steps":1}"#,
                StatusCode::ACCEPTED,
            ),
        ] {
            let response = router
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri(path)
                        .header("host", "[::1]:32145")
                        .header("authorization", format!("Bearer {TOKEN}"))
                        .header(CORE_API_VERSION_HEADER, "1")
                        .header("content-type", "application/json")
                        .body(Body::from(body))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), expected);
        }
    }
}
