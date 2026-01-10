# Type-Aware Initialization Checking

## Problem Statement

Pyrefly's initialization checking currently happens at binding time, before type
information is available. This causes false negatives when branches have
termination keys (potential `Never`/`NoReturn` calls) that aren't yet known to
actually terminate.

### Example: False Negative

```python
from typing import Literal, NoReturn

def never() -> NoReturn:
    raise Exception()

def test_non_exhaustive(x: Literal[1, 2, 3]) -> str:
    match x:  # NOT exhaustive - missing case 3
        case 1:
            y = "one"
        case 2:
            never()  # y not initialized, but has termination_key
    return y  # BUG: Should flag "may be uninitialized" but doesn't
```

**Why it happens**: At binding time, the flow merger counts branches with
`termination_key` as "potentially terminating" via this heuristic:

```rust
let this_name_always_defined = n_values == n_branches
    || n_missing_branches <= n_branches_with_termination_key;
```

This assumes any branch with a termination key will terminate, but we don't know
the actual return type until solve time.

### Contrast: Correct Behavior Without Termination Key

```python
def test_correct(x: Literal[1, 2, 3]) -> str:
    match x:
        case 1:
            y = "one"
        case 2:
            pass  # No termination key
    return y  # Correctly flags: "`y` may be uninitialized"
```

## Design Goals

1. **Fix the false negative**: When termination_key doesn't resolve to Never,
   correctly flag uninitialized variables
2. **Preserve current behavior**: No regressions for cases that work correctly
3. **Maintain coherent architecture**: Use patterns consistent with existing
   termination_key handling in Phi nodes
4. **Minimize scope**: Target the specific gap without overhauling initialization

## Solution: Conditional Initialization State

### Core Idea

Instead of making a binding-time decision about whether a branch "counts" toward
initialization, we defer the decision to solve time by recording the dependency:

> "This variable is initialized **if** this termination_key returns Never;
> otherwise, it may be uninitialized."

This mirrors how Phi already handles type merging with termination_key.

## Implementation Plan

### Phase 1: Extend InitializedInFlow

**File**: `pyrefly/lib/binding/bindings.rs`

Add a new variant to represent deferred initialization checks:

```rust
/// Represents whether a name is initialized at a given point in the flow.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InitializedInFlow {
    /// Definitely initialized
    Yes,
    /// May or may not be initialized (from branch merging)
    Conditionally,
    /// Definitely not initialized
    No,
    /// NEW: Initialized if termination_key returns Never; otherwise use fallback
    ConditionalOnTermination {
        /// The key to check at solve time (e.g., a StmtExpr for a function call)
        termination_key: Idx<Key>,
        /// What initialization state to use if termination_key does NOT return Never
        fallback: Box<InitializedInFlow>,
    },
}

impl InitializedInFlow {
    /// Returns an error message if this state indicates an uninitialized variable.
    ///
    /// NOTE: This should NOT be called for ConditionalOnTermination - that variant
    /// must be resolved at solve time first.
    pub fn as_error_message(&self, name: &Name) -> Option<String> {
        match self {
            InitializedInFlow::Yes => None,
            InitializedInFlow::Conditionally => {
                Some(format!("`{name}` may be uninitialized"))
            }
            InitializedInFlow::No => {
                Some(format!("`{name}` is uninitialized"))
            }
            InitializedInFlow::ConditionalOnTermination { .. } => {
                // This should be resolved at solve time before calling as_error_message
                panic!("ConditionalOnTermination must be resolved at solve time")
            }
        }
    }

    /// Combine two initialization states (for merging branches)
    pub fn merge(self, other: Self) -> Self {
        match (self, other) {
            (InitializedInFlow::Yes, InitializedInFlow::Yes) => InitializedInFlow::Yes,
            (InitializedInFlow::No, InitializedInFlow::No) => InitializedInFlow::No,
            // Any conditional makes the result conditional
            _ => InitializedInFlow::Conditionally,
        }
    }
}
```

### Phase 2: Modify Flow Merging

**File**: `pyrefly/lib/binding/scope.rs`

Update the flow merging logic to create `ConditionalOnTermination` when
appropriate, instead of optimistically counting termination keys.

#### Current Logic (simplified)

```rust
// In merged_flow_info or similar
let n_branches_with_termination_key = flows
    .iter()
    .filter(|f| f.last_stmt_expr.is_some())
    .count();

let this_name_always_defined = n_values == n_branches
    || n_missing_branches <= n_branches_with_termination_key;
```

#### New Logic

```rust
/// Compute the merged initialization state for a name across branches.
fn compute_merged_initialization(
    &self,
    name: &Name,
    branches: &[Flow],
    base_flow: &Flow,
    base_termination_key: Option<Idx<Key>>,
) -> InitializedInFlow {
    // Collect (has_value, termination_key) for each branch including base
    let mut branch_states: Vec<(bool, Option<Idx<Key>>)> = Vec::new();

    // Add base flow state
    let base_has_value = base_flow.get_value(name).is_some();
    branch_states.push((base_has_value, base_termination_key));

    // Add each branch's state
    for flow in branches {
        let has_value = flow.get_value(name).is_some();
        let term_key = flow.last_stmt_expr;
        branch_states.push((has_value, term_key));
    }

    // If all branches have the value, we're definitely initialized
    if branch_states.iter().all(|(has_value, _)| *has_value) {
        return InitializedInFlow::Yes;
    }

    // Check if all branches that DON'T have the value have termination keys
    let missing_branches: Vec<_> = branch_states
        .iter()
        .filter(|(has_value, _)| !has_value)
        .collect();

    if missing_branches.is_empty() {
        return InitializedInFlow::Yes;
    }

    // Build a chain of ConditionalOnTermination for branches with termination keys
    let mut result = InitializedInFlow::Conditionally;  // Default fallback

    for (has_value, term_key) in missing_branches.iter().rev() {
        if let Some(key) = term_key {
            // This branch might terminate - defer the check
            result = InitializedInFlow::ConditionalOnTermination {
                termination_key: *key,
                fallback: Box::new(result),
            };
        }
        // If no termination key and no value, result stays Conditionally
    }

    // If ALL missing branches have termination keys, the outermost result
    // will be ConditionalOnTermination; if any don't, it will collapse to
    // Conditionally at some point in the chain.
    result
}
```

### Phase 3: Store Initialization State in Bindings

**File**: `pyrefly/lib/binding/binding.rs`

We need to thread `InitializedInFlow` to solve time. The cleanest approach is to
extend `Binding::Forward`:

```rust
pub enum Binding {
    // ... existing variants ...

    /// A forward reference to another binding.
    ///
    /// The second field tracks initialization state, which may need to be
    /// resolved at solve time if it contains ConditionalOnTermination.
    Forward(Idx<Key>, InitializedInFlow),

    // ... rest of variants ...
}
```

**Update all Forward creation sites** to include the initialization state:

```rust
// In expr.rs, when looking up a name for read:
NameLookupResult::Found { idx: value, initialized } => {
    // Instead of raising error here, store the state for solve-time checking
    self.insert_binding(key, Binding::Forward(value, initialized))
}
```

### Phase 4: Resolve Initialization at Solve Time

**File**: `pyrefly/lib/alt/solve.rs`

Add a helper to resolve conditional initialization:

```rust
impl<'a, 'b> AnswersSolver<'a, 'b> {
    /// Resolve an InitializedInFlow that may contain ConditionalOnTermination.
    ///
    /// This checks termination keys at solve time to determine the actual
    /// initialization state.
    fn resolve_initialization(&self, init: &InitializedInFlow) -> InitializedInFlow {
        match init {
            InitializedInFlow::ConditionalOnTermination { termination_key, fallback } => {
                let term_type = self.get_idx(*termination_key);
                if term_type.ty().is_never() {
                    // This branch terminates, so it doesn't contribute to initialization
                    // Check if there are more conditions in the fallback
                    self.resolve_initialization(fallback)
                } else {
                    // This branch does NOT terminate, so it matters for initialization
                    // The fallback tells us what happens in that case
                    self.resolve_initialization(fallback)
                }
            }
            other => other.clone(),
        }
    }
}
```

Wait, the logic above isn't quite right. Let me reconsider...

The semantics should be:
- `ConditionalOnTermination { key, fallback }` means:
  - If `key` returns Never → this branch is dead, ignore it for initialization
  - If `key` does NOT return Never → this branch is live, and contributes uninitialized state

So the resolution should be:

```rust
fn resolve_initialization(&self, init: &InitializedInFlow) -> InitializedInFlow {
    match init {
        InitializedInFlow::ConditionalOnTermination { termination_key, fallback } => {
            let term_type = self.get_idx(*termination_key);
            if term_type.ty().is_never() {
                // Branch terminates - doesn't contribute to initialization concern
                // If fallback is Yes or another ConditionalOnTermination, continue checking
                // If fallback is Conditionally, that's from OTHER branches
                self.resolve_initialization(fallback)
            } else {
                // Branch does NOT terminate - we have an uninitialized path
                InitializedInFlow::Conditionally
            }
        }
        other => other.clone(),
    }
}
```

Actually, let me think about this more carefully with an example:

```python
match x:  # Literal[1, 2, 3]
    case 1:
        y = "one"    # initialized
    case 2:
        never()      # termination_key, NOT initialized
    case 3:
        pass         # NOT initialized, NO termination_key
```

The merged state for `y` should be:
- Branch 1: initialized
- Branch 2: not initialized, has termination_key
- Branch 3: not initialized, no termination_key
- Base flow: not initialized (match not exhaustive), has termination_key (MatchExhaustive)

At binding time:
```
ConditionalOnTermination {
    termination_key: MatchExhaustive,  // base flow
    fallback: ConditionalOnTermination {
        termination_key: never_call,  // case 2
        fallback: Conditionally,  // case 3 has no termination key
    }
}
```

At solve time:
1. Check MatchExhaustive: returns Literal[3] (not Never) → base flow is live
2. Since base flow is live and uninitialized → result is Conditionally

Actually wait, this isn't quite right either. The structure needs to represent
"all of these termination keys must return Never for initialization to be OK."

Let me reconsider the data structure:

```rust
/// Initialization state that may depend on solve-time type information.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InitializedInFlow {
    /// Definitely initialized
    Yes,
    /// May or may not be initialized
    Conditionally,
    /// Definitely not initialized
    No,
    /// Initialized only if ALL of these termination keys return Never.
    /// If any termination key does NOT return Never, the variable may be uninitialized.
    ConditionalOnAllTerminating {
        /// Termination keys that must all return Never
        termination_keys: SmallVec<[Idx<Key>; 2]>,
    },
}

impl InitializedInFlow {
    /// Resolve at solve time by checking all termination keys.
    fn resolve(&self, get_type: impl Fn(Idx<Key>) -> &Type) -> InitializedInFlow {
        match self {
            InitializedInFlow::ConditionalOnAllTerminating { termination_keys } => {
                // All keys must return Never for the variable to be initialized
                let all_terminate = termination_keys
                    .iter()
                    .all(|k| get_type(*k).is_never());

                if all_terminate {
                    InitializedInFlow::Yes
                } else {
                    InitializedInFlow::Conditionally
                }
            }
            other => other.clone(),
        }
    }
}
```

This is simpler! We just collect all the termination keys from branches that
don't initialize the variable, and at solve time check if they ALL terminate.

### Phase 4 (Revised): Resolve Initialization at Solve Time

**File**: `pyrefly/lib/alt/solve.rs`

Update the handling of `Binding::Forward`:

```rust
Binding::Forward(target, initialized) => {
    // Resolve any conditional initialization at solve time
    let resolved = match initialized {
        InitializedInFlow::ConditionalOnAllTerminating { termination_keys } => {
            let all_terminate = termination_keys
                .iter()
                .all(|k| self.get_idx(*k).ty().is_never());

            if all_terminate {
                InitializedInFlow::Yes
            } else {
                InitializedInFlow::Conditionally
            }
        }
        other => other.clone(),
    };

    // Report error if uninitialized
    if !self.module().path().is_interface() {
        if let Some(msg) = resolved.as_error_message(&name) {
            self.error(
                errors,
                range,  // Need to thread this through - see Phase 5
                ErrorInfo::Kind(ErrorKind::UnboundName),
                msg,
            );
        }
    }

    // Return the type from the target binding
    self.get_idx(*target).arc_clone()
}
```

### Phase 5: Thread Name and Range to Solve Time

The `Forward` binding needs the name and range for error messages:

```rust
pub enum Binding {
    // ... existing variants ...

    /// A forward reference to another binding, with initialization tracking.
    Forward {
        target: Idx<Key>,
        initialized: InitializedInFlow,
        name: Name,      // For error message
        range: TextRange, // For error location
    },

    // ... rest of variants ...
}
```

Update creation site in `expr.rs`:

```rust
NameLookupResult::Found { idx: value, initialized } => {
    // DON'T raise error here anymore - defer to solve time
    self.insert_binding(
        key,
        Binding::Forward {
            target: value,
            initialized,
            name: name.id.clone(),
            range: name.range,
        }
    )
}
```

### Phase 6: Disable Binding-Time Error for Deferred Cases

**File**: `pyrefly/lib/binding/expr.rs`

For `ConditionalOnAllTerminating` cases, we must NOT raise errors at binding
time since we don't know the answer yet:

```rust
NameLookupResult::Found { idx: value, initialized } => {
    // Only raise binding-time errors for definite cases
    match &initialized {
        InitializedInFlow::ConditionalOnAllTerminating { .. } => {
            // Defer to solve time - don't error here
        }
        _ => {
            if !used_in_static_type
                && !self.module_info.path().is_interface()
                && let Some(error_message) = initialized.as_error_message(&name.id)
            {
                self.error(name.range, ErrorInfo::Kind(ErrorKind::UnboundName), error_message);
            }
        }
    }

    self.insert_binding(
        key,
        Binding::Forward {
            target: value,
            initialized,
            name: name.id.clone(),
            range: name.range,
        }
    )
}
```

## Testing Plan

### Test Case 1: Non-Exhaustive Match with Never (The Original Bug)

```python
# Should error: "`y` may be uninitialized"
from typing import Literal, NoReturn

def never() -> NoReturn: ...

def test(x: Literal[1, 2, 3]) -> str:
    match x:
        case 1:
            y = "one"
        case 2:
            never()
    return y  # E: `y` may be uninitialized
```

### Test Case 2: Exhaustive Match with Never (Should Pass)

```python
# Should NOT error - match is exhaustive
from typing import Literal, NoReturn

def never() -> NoReturn: ...

def test(x: Literal[1, 2]) -> str:
    match x:
        case 1:
            y = "one"
        case 2:
            never()
    return y  # OK - case 2 terminates, y is always initialized
```

### Test Case 3: If-Else with Never (Should Pass)

```python
# Should NOT error - else terminates
from typing import NoReturn

def never() -> NoReturn: ...

def test(cond: bool) -> str:
    if cond:
        y = "value"
    else:
        never()
    return y  # OK
```

### Test Case 4: Variable Used Only in Branch (No False Positive)

```python
# Should NOT error - y is never used after the if
def test(cond: bool) -> int:
    if cond:
        y = 1
        print(y)
    return 0  # OK - y not used here
```

### Test Case 5: Multiple Termination Keys, One Fails

```python
# Should error - case 3 doesn't terminate
from typing import Literal, NoReturn

def never() -> NoReturn: ...
def maybe_never(x: int) -> int: ...  # NOT NoReturn

def test(x: Literal[1, 2, 3]) -> str:
    match x:
        case 1:
            y = "one"
        case 2:
            never()  # Terminates
        case 3:
            maybe_never(0)  # Does NOT terminate
    return y  # E: `y` may be uninitialized
```

### Test Case 6: Nested Conditionals

```python
from typing import Literal, NoReturn

def never() -> NoReturn: ...

def test(x: Literal[1, 2], flag: bool) -> str:
    if flag:
        match x:
            case 1:
                y = "one"
            case 2:
                never()
    else:
        y = "default"
    return y  # OK - all paths initialize y or terminate
```

## Migration Strategy

### Step 1: Add Data Structures (No Behavior Change)

1. Add `ConditionalOnAllTerminating` variant to `InitializedInFlow`
2. Extend `Binding::Forward` to include initialization state, name, and range
3. Update all `Binding::Forward` creation sites
4. Run tests - expect no behavior changes

### Step 2: Wire Up Flow Merging

1. Update `compute_merged_initialization` to use `ConditionalOnAllTerminating`
2. Ensure base flow termination key (MatchExhaustive) is included
3. Run tests - still expect no behavior changes (errors still at binding time)

### Step 3: Add Solve-Time Checking

1. Add resolution logic in `Binding::Forward` handling
2. For `ConditionalOnAllTerminating`, check all termination keys
3. Run tests - expect the false negative test case to now pass

### Step 4: Cleanup

1. Consider removing redundant binding-time checks for non-conditional cases
2. Update documentation
3. Final test pass

## Risks and Mitigations

### Risk: Performance

Adding `SmallVec<[Idx<Key>; 2]>` to every `Forward` binding increases memory.

**Mitigation**: Use `Option<SmallVec<...>>` or a separate variant so the common
case (definitely initialized) has no overhead.

### Risk: Error Message Regression

Moving error emission to solve time might affect message quality or location.

**Mitigation**: We explicitly thread `name` and `range` through the binding,
so error messages should be identical.

### Risk: Duplicate Errors

If a variable is read multiple times while uninitialized, we might emit
multiple errors.

**Mitigation**: The current system also emits per-read errors, so this isn't
a regression. Could add deduplication later if desired.

## Future Work

- **LoopPhi handling**: Loop initialization has additional complexity with
  `MergeStyle::Loop` and `MergeStyle::LoopDefinitelyRuns`. This proposal
  focuses on branch/match cases.

- **Attribute initialization**: Class attribute initialization checking could
  potentially use similar patterns.

- **Full solve-time initialization**: This proposal is a targeted fix. A more
  comprehensive redesign could move all initialization tracking to solve time,
  but that's higher risk and complexity.

## Appendix: File Changes Summary

| File | Changes |
|------|---------|
| `pyrefly/lib/binding/bindings.rs` | Add `ConditionalOnAllTerminating` variant |
| `pyrefly/lib/binding/binding.rs` | Extend `Binding::Forward` with init state, name, range |
| `pyrefly/lib/binding/scope.rs` | Update flow merging to create conditional init states |
| `pyrefly/lib/binding/expr.rs` | Defer error for conditional cases, update Forward creation |
| `pyrefly/lib/alt/solve.rs` | Add resolution logic and error emission for Forward |
| `pyrefly/lib/test/flow_branching.rs` | Add test cases |
