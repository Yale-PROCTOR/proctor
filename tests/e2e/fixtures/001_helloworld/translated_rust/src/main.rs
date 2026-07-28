extern "C" {
    fn printf(__format: *const i8, ...)
    -> i32;
}
unsafe fn main_0() -> i32 {
    printf(b"Hello World!\n\0" as *const u8 as *const i8);
    return 0;
}
pub fn main() { unsafe { ::std::process::exit(main_0()) } }
