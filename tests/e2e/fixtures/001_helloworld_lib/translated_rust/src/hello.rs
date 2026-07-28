extern "C" {
    fn printf(__format: *const i8, ...)
    -> i32;
}
#[no_mangle]
pub unsafe extern "C" fn helloworld() -> i32 {
    printf(b"Hello World!\n\0" as *const u8 as *const i8);
    return 0;
}
