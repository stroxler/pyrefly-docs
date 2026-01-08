# Transactional Error Collection in Pyrefly

## Problem

When multiple threads (or the same thread via cycle resolution) compute the same binding, they all add errors to the shared `base_errors` collector. Since `Calculation::record_value` uses "first write wins" semantics for the answer, but errors are added during computation (before the write), we can get duplicate errors.

**Current flow in `calculate_and_record_answer`:**
```rust
let answer = calculation.record_value(
    K::solve(self, binding, self.base_errors),  // errors added here, before we know if we "win"
    |var, answer| self.finalize_recursive_answer::<K>(idx, var, answer)
);
```

## Solution

Make error collection "transactional": collect errors into a fresh collector during solve, then only transfer them to `base_errors` when we actually commit the answer.

## Key Files

- `crates/pyrefly_graph/src/calculation.rs` - `Calculation::record_value` API
- `pyrefly/lib/alt/answers_solver.rs` - `calculate_and_record_answer`, `finalize_recursive_answer`
- `pyrefly/lib/error/collector.rs` - `ErrorCollector::extend` (already exists)

## Implementation Plan

### Step 1: Modify `Calculation::record_value` to indicate write status

**File:** `crates/pyrefly_graph/src/calculation.rs`

Change return type to include whether we were the writer:

```rust
/// Returns (final_value, did_write) where did_write is true if this call wrote the value
pub fn record_value(&self, value: T, on_recursive: impl FnOnce(R, T) -> T) -> (T, bool) {
    let mut lock = self.0.lock();
    match &mut *lock {
        Status::Calculating(calc) => {
            // WE are the writer
            let rec = &mut calc.0;
            let value = match rec.take() {
                Some(r) => on_recursive(r, value),
                None => value,
            };
            *lock = Status::Calculated(value.dupe());
            (value, true)  // we wrote
        }
        Status::Calculated(v) => {
            (v.dupe(), false)  // another thread wrote
        }
        // ...
    }
}
```

### Step 2: Update `calculate_and_record_answer` to use local error collector

**File:** `pyrefly/lib/alt/answers_solver.rs`

```rust
fn calculate_and_record_answer<K: Solve<Ans>>(
    &self,
    current: CalcId,
    idx: Idx<K>,
    calculation: &Calculation<Arc<K::Answer>, Var>,
) -> Arc<K::Answer> {
    let binding = self.bindings().get(idx);

    // Create a fresh error collector for this solve attempt
    let local_errors = self.error_collector();

    let (answer, did_write) = calculation.record_value(
        K::solve(self, binding, &local_errors),
        |var, answer| {
            // finalize_recursive_answer also needs the local collector
            self.finalize_recursive_answer_with_errors::<K>(idx, var, answer, &local_errors)
        }
    );

    // Only transfer errors if we were the thread that wrote the answer
    if did_write {
        self.base_errors.extend(local_errors);
    }

    self.cycles().on_calculation_finished(&current);
    answer
}
```

### Step 3: Update `finalize_recursive_answer` to accept error collector

**File:** `pyrefly/lib/alt/answers_solver.rs`

Either:
- Add a new method `finalize_recursive_answer_with_errors` that takes `errors: &ErrorCollector`
- Or modify the existing method signature

```rust
fn finalize_recursive_answer_with_errors<K: Solve<Ans>>(
    &self,
    idx: Idx<K>,
    var: Var,
    answer: Arc<K::Answer>,
    errors: &ErrorCollector,  // use this instead of self.base_errors
) -> Arc<K::Answer> {
    let range = self.bindings().idx_to_key(idx).range();
    let final_answer = K::record_recursive(self, range, answer, var, errors);
    if var != Var::ZERO {
        self.solver().force_var(var);
    }
    final_answer
}
```

### Step 4: Update callers of `record_value`

Search for other uses of `calculation.record_value` that may need updating. Based on exploration, the main usage is in `calculate_and_record_answer`.

The `calculate` helper method in `calculation.rs` also uses `record_value` internally - this can continue to ignore the bool since it doesn't deal with error collection.

### Step 5: Handle the duplicate cycle error case

In `get_idx`, there's a direct `self.base_errors.add()` call for duplicate cycle detection (lines 568-574). This is fine to keep as-is since it's a special error case that only fires once per duplicate cycle, not during normal solving.

## Testing

1. Existing tests should continue to pass (no behavior change for single-threaded cases)
2. Consider adding a test that verifies errors aren't duplicated when the same binding is computed multiple times (though this may be hard to trigger deterministically)

## Migration Notes

- The `record_value` return type change affects all callers
- Callers that don't care about the bool can use `let (value, _) = calculation.record_value(...)`
- The `calculate` helper method in `calculation.rs` should be updated to handle the new return type internally
