+++
id = "local_transformation"
version = 1
description = "Transform one Rust function SCC against Crat skeletons."
variables = ["dependency_context", "transformation_targets", "repair_context", "repair_base", "use_xj_scanf_guidance", "libc_guidance", "printf_guidance", "proctor_libc_call_guidance", "foreign_static_guidance"]
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

Closures, and blocks or `if` expressions nested inside operands, are opaque
regions belonging to their enclosing transformation region. Their internal
statements and bindings receive no independent labels or target local-variable
types; closure parameters follow the same rule. Rewrite these internals as
needed while preserving behavior and using the target types of referenced
outer bindings. Direct control statements, initializers, return/break values,
and match-arm results retain their existing labeled structure. A return,
break, or continue must not escape an opaque value block or conditional.

Requirements:

1. Exactly preserve source behavior wherever it is defined, including apparent
   bugs. Do not add validation, fallback behavior, or error handling absent
   from the source; preserve its preconditions. For example, if the source
   immediately dereferences a raw pointer, directly unwrap the corresponding
   `Option` instead of adding a conditional check.
2. Use the target skeleton's exact lifetime-generic declaration, parameter
   types, return type, and local-variable types.
3. Call transformed function dependencies using their target signatures.
4. Outside opaque regions, keep every existing function, parameter, and
   local-binding name. Preserve each existing declaration exactly once in the
   same label, pattern, and control-flow role.
5. Outside opaque regions, name every new local binding
   `proctor_temp_var_n`, where `n` is a nonnegative integer. Use it only within
   the consecutive statements carrying the same `#[proctor(N)]` label that
   encloses its declaration, including unlabeled code nested within those
   statements.
6. Do not define a function, type, static, constant, module, or other item
   inside a transformation target.
7. At each labeled statement-list level, preserve every source
   `#[proctor(N)]` label in order. A labeled statement may expand only into one
   or more consecutive sibling statements with the same label. Do not insert
   unlabeled siblings at that level, repeat a label in nested code, or label
   newly introduced nested statements.
8. Outside opaque regions, preserve each existing control form, its direct
   role, its branch/arm/guard/body structure, and all existing nested labels.
   Plain blocks, `if`, `if let`, `while`, `while let`, `for`, `loop`, and `match`
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

{% set scanf_guidance %}{% include "partials/xj_scanf.jinja" %}{% endset %}{{ scanf_guidance | trim | indent(4, true) }}{% endif %}{% if libc_guidance %}

    {{ libc_guidance | replace('\n', '\n    ') }}{% endif %}{% if printf_guidance %}

    {{ printf_guidance | replace('\n', '\n    ') }}{% endif %}{% if proctor_libc_call_guidance %}

    {{ proctor_libc_call_guidance | replace('\n', '\n    ') }}{% endif %}{% if foreign_static_guidance %}

    {{ foreign_static_guidance | replace('\n', '\n    ') }}{% endif %}
11. {% set cast_guidance %}{% include "partials/bytemuck.jinja" %}{% endset %}{{ cast_guidance | trim | indent(4) }}
12. Do not introduce an explicit `unsafe` block or a statement or expression
    attribute other than the required `#[proctor(N)]` labels.
{% if repair_base is not none %}13. {% set patch_guidance %}{% include "partials/repair_patch.jinja" %}{% endset %}{{ patch_guidance | trim | indent(4) }}
{% else %}13. Return exactly one Rust code block delimited by triple-backtick fences.
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

{% endif %}{% if dependency_context %}## Dependency Context

{{ dependency_context }}

{% endif %}## Transformation Targets

{{ transformation_targets }}

{{ repair_context -}}
