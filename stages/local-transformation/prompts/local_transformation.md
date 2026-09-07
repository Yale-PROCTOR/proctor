+++
id = "local_transformation"
version = 1
description = "Transform one Rust function SCC against Crat skeletons."
variables = ["dependency_context", "transformation_targets", "repair_context", "use_xj_scanf_guidance", "libc_guidance"]
+++
You are transforming unsafe Rust functions generated from C.

The source code is the original implementation. The target skeleton defines
the transformation goal. A dependency's source signature is its signature
before transformation; its target signature is how transformed code must call
it.

Implement every function in Transformation Targets exactly once. Emit no
other top-level item. Use Dependency Context only as reference; do not emit or
redefine its functions, types, statics, or constants.

Complete every generated `todo!()` hole. Preserve every complete labeled statement already present in the Target Skeleton exactly as provided.

Requirements:

1. Exactly preserve source behavior wherever it is defined, including apparent
   bugs. Do not add validation, fallback behavior, or error handling absent
   from the source; preserve its preconditions. For example, if the source
   immediately dereferences a raw pointer, directly unwrap the corresponding
   `Option` instead of adding a conditional check.
2. Use the target skeleton's exact lifetime-generic declaration, parameter
   types, return type, and local-variable types.
3. Call transformed function dependencies using their target signatures.
4. Keep every existing function, parameter, and local-binding name. Preserve
   each existing declaration exactly once in the same label, pattern, and
   control-flow role.
5. Name every new local binding `proctor_temp_var_n`, where `n` is a
   nonnegative integer. Use it only within the consecutive statements carrying
   the same `#[proctor(N)]` label that encloses its declaration, including
   unlabeled code nested within those statements.
6. Do not define a function, type, static, constant, module, or other item
   inside a transformation target.
7. At each existing statement-list level, preserve every source
   `#[proctor(N)]` label in order. A labeled statement may expand only into one
   or more consecutive sibling statements with the same label. Do not insert
   unlabeled siblings at that level, repeat a label in nested code, or label
   newly introduced nested statements.
8. Preserve each existing control form, its direct role, its
   branch/arm/guard/body structure, and all existing nested labels. Plain
   blocks, `if`, `if let`, `while`, `while let`, `for`, `loop`, and `match`
   are distinct. A `let ... else` must remain a `let ... else`. A control form
   used directly as a `let` initializer, `return` value, `break` value, or
   match-arm result must remain in that role. Conditions, scrutinees, patterns,
   and statement contents may be rewritten.
9. If a labeled statement containing a control form expands into multiple
   same-label siblings, exactly one sibling must preserve that form, role, and
   all its existing labeled nested statements. Other siblings must not have a
   control form in that same direct role and must contain no labels below their
   own group label.
10. For each listed foreign-function reference, prefer a behavior-equivalent
    safe Rust function or method when one is available; otherwise preserve the
    foreign call.{% if use_xj_scanf_guidance %}

    When a listed foreign reference is `scanf`, `fscanf`, or `sscanf`, use
    `xj_scanf::legacy::scanf`, `xj_scanf::legacy::brscanf`, or
    `xj_scanf::legacy::bscanf`, respectively. Whenever the format, target
    types, and transformed input type are supported, replace the foreign call
    with that function instead of reimplementing scanning. Otherwise preserve
    the foreign call. The function signatures are:

    ```rust
    pub fn scanf(
        format: &str,
        args: &mut [&mut dyn xj_scanf::legacy::ScanTarget],
    ) -> i32;
    pub fn brscanf<R: std::io::BufRead>(
        reader: R,
        format: &str,
        args: &mut [&mut dyn xj_scanf::legacy::ScanTarget],
    ) -> i32;
    pub fn bscanf(
        input: &[u8],
        format: &str,
        args: &mut [&mut dyn xj_scanf::legacy::ScanTarget],
    ) -> i32;
    ```

    Integer conversions are `%d`, `%i`, `%o`, `%u`, `%x`, and `%X`; `%i`
    detects decimal, octal, or hexadecimal prefixes. Floating-point
    conversions are `%f`, `%F`, `%e`, `%E`, `%g`, `%G`, `%a`, and `%A`. `%c`
    reads a fixed number of characters, defaulting to one; `%s` reads
    non-whitespace characters; and `%[...]` and `%[^...]` match a character
    set or its inverse. `%n` stores the number of characters consumed, and
    `%%` matches a literal percent sign. `*` suppresses assignment, a decimal
    field width limits the bytes scanned, and the length modifiers are `hh`
    for integer `char`, `h` for integer `short`, `l` for integer `long` or
    floating-point `double`, `ll` for integer `long long`, `L` for
    floating-point `long double`, `j` for integer `intmax_t`, `t` for integer
    `ptrdiff_t`, and `z` for integer `size_t`.

    Supply mutable targets in conversion order.
    `xj_scanf::legacy::ScanTarget` is implemented for `i8`, `i16`, `i32`,
    `i64`, `u8`, `u16`, `u32`, `u64`, `usize`, `f32`, `f64`, `char`, `String`,
    `Vec<u8>`, and `&mut [u8]`; storing fails when a scanned value's type does
    not match its target. The functions return the number of successful
    assignments, `0` when available input fails the first conversion, and
    `-1` for EOF before any conversion. `scanf` reads standard input,
    `brscanf` accepts a `std::io::BufRead`, and `bscanf` accepts a byte slice.

    These signatures are reference material only. Do not define or import any
    item for these calls, including `ScanTarget`. Call the selected function
    through its fully qualified path. For example, with `input: &[u8]`,
    `x: i32`, and `y: f32`:

    ```rust
    xj_scanf::legacy::bscanf(input, "%d %f", &mut [&mut x, &mut y])
    ```{% endif %}{% if libc_guidance %}

    {{ libc_guidance | replace('\n', '\n    ') }}{% endif %}
11. When casting between references or slices, avoid unsafe code by using these
    `bytemuck` functions whenever they preserve behavior for inputs on which
    the source behavior is defined:

    ```rust
    pub fn cast_mut<A: NoUninit + AnyBitPattern, B: NoUninit + AnyBitPattern>(
        a: &mut A,
    ) -> &mut B;
    pub fn cast_ref<A: NoUninit, B: AnyBitPattern>(a: &A) -> &B;
    pub fn cast_slice<A: NoUninit, B: AnyBitPattern>(a: &[A]) -> &[B];
    pub fn cast_slice_mut<A: NoUninit + AnyBitPattern, B: NoUninit + AnyBitPattern>(
        a: &mut [A],
    ) -> &mut [B];
    ```

    It is acceptable for these calls to panic only on inputs that would make
    the corresponding source access undefined behavior, such as a misaligned
    dereference. Do not avoid `bytemuck` merely because such undefined inputs
    can panic. For defined inputs, scalar reference casts require equal source
    and destination sizes, and slice casts require the total byte length to
    form a whole number of destination elements. These signatures are
    reference material only. Do not define or import the functions, and do not
    declare an `extern crate`. Call them through their fully qualified paths,
    such as `bytemuck::cast_ref(e)`.
12. Do not introduce an explicit `unsafe` block or a statement or expression
    attribute other than the required `#[proctor(N)]` labels.
13. Return exactly one Rust code block delimited by triple-backtick fences.
    Include all requested functions and no prose. Do not use tilde or
    longer-backtick fences.

Example:

Source:

```rust
unsafe fn read_value(mut p: *const i32, mut q: *const i32) -> i32 {
    #[proctor(0)]
    let mut x: i32 = *p.add(1);
    #[proctor(1)]
    return if q.is_null() {
        #[proctor(2)]
        x
    } else {
        #[proctor(3)]
        x + *q
    };
}
```

Target skeleton:

```rust
unsafe fn read_value(mut p: &[i32], mut q: Option<&i32>) -> i32 {
    #[proctor(0)]
    let mut x: i32 = todo!();
    #[proctor(1)]
    return if todo!() {
        #[proctor(2)]
        todo!()
    } else {
        #[proctor(3)]
        todo!()
    };
}
```

Valid output:

```rust
unsafe fn read_value(mut p: &[i32], mut q: Option<&i32>) -> i32 {
    #[proctor(0)]
    let mut x: i32 = p[1];
    #[proctor(1)]
    return if q.is_none() {
        #[proctor(2)]
        x
    } else {
        #[proctor(3)]
        x + *q.unwrap()
    };
}
```

{% if dependency_context %}## Dependency Context

{{ dependency_context }}

{% endif %}## Transformation Targets

{{ transformation_targets }}

{{ repair_context -}}
