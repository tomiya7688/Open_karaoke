fn main() {
    println!("Open Karaoke Core bootstrap OK");
}

#[cfg(test)]
mod tests {
    #[test]
    fn bootstrap_smoke_test() {
        assert_eq!(48_000_u32, 48_000_u32);
    }
}
