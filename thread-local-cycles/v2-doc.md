# Transactional Var Pinning: Fresh Implementation Guide

This document provides a complete implementation guide for eliminating nondeterminism in
Pyrefly's cycle resolution. It assumes a fresh start on top of the transactional errors
implementation already on trunk.

---

## Problem Statement

Pyrefly has nondeterminism issues when multiple threads compute the same binding in a cycle:

1. All threads call `K::solve` and generate errors
2. Only the first thread to call `record_value` "wins"
3. Other threads' values and errors are discarded (or worse, duplicated)

**Root cause:** Non-idempotent computation. Computing with placeholder types produces different
results (and errors) than computing with final resolved types.

---

## What's Already Implemented

**Transactional Errors (D90268296):** The `record_value` method now returns `(T, bool)`:

```rust
pub fn record_value(&self, value: T, on_recursive: impl FnOnce(R, T) -> T) -> (T, bool) {
    // Returns (value, true) if we wrote our value
    // Returns (existing_value, false) if another thread's value was used
}
```

In `calculate_and_record_answer`, errors are buffered locally and only committed when
`record_value` returns `true`:

```rust
let error_buffer = ErrorCollector::new(...);
let computed_answer = K::solve(self, binding, &error_buffer);
let (answer, we_wrote) = calculation.record_value(computed_answer, ...);
if we_wrote {
    self.base_errors.extend(error_buffer);
}
```

This solves multi-thread error duplication for **non-cycle** cases. For cycles, we need
additional work because pass 1 (tentative) and pass 2 (canonical) both generate errors.

---

## Solution: Two-Pass Cycle Resolution

### The Three-Value Model

For each cycle with break_at binding B:

1. **T_B0** (placeholder): `Any` or `Variable::Recursive`, stored when cycle detected
2. **T_B1** (tentative): Result of pass 1 using T_B0
3. **T_B2** (canonical): Result of pass 2 using T_B1, **this is what gets committed**

### The Core Invariant

**Pass N uses Pass N-1 result for break_at.**

This ensures all bindings in the cycle observe a consistent value for break_at.

### Two-Pass Protocol

**Pass 1 (Tentative):**
- Store T_B0 in PreliminaryAnswers when cycle detected
- Compute break_at and all dependencies
- Store ALL results in PreliminaryAnswers only (NOT in global Calculation)
- Errors from pass 1 are NOT committed (because no record_value call)

**Pass 2 (Canonical):**
- Keep T_B1 in PreliminaryAnswers for break_at
- Track all non-break_at participants as "pass 2 participants" in ThreadState
- Recompute by calling K::solve on break_at
- When dependencies are accessed via get_idx:
  - If it's a pass 2 participant: bypass propose_calculation, compute directly, store in PreliminaryAnswers
  - If it's break_at: return T_B1 from PreliminaryAnswers
  - Otherwise: use normal Calculation path
- After pass 2 completes: call record_value for ALL participants to finalize
- Errors from pass 2 ARE committed (via transactional error mechanism when record_value succeeds)

**Result:** Deterministic errors based on resolved types, not placeholders.

---

## Key Implementation Challenge: Calculation States

### Understanding the Problem

The `Calculation` state machine has three states:

```rust
enum Status<T, R> {
    NotCalculated,
    Calculating(CalculatingStatus<R>),  // Contains thread ID
    Calculated(T),
}
```

When a binding B is being computed:
1. `propose_calculation(B)` transitions: NotCalculated → Calculating(thread_id)
2. If B depends on D, D also transitions to Calculating
3. If D depends back on B, `propose_calculation(B)` sees Calculating with same thread_id → CycleDetected

**Critical insight:** During cycle resolution, all participants are in `Calculating` state,
not `NotCalculated`. This affects how pass 2 works.

### The State Flow

**During pass 1:**
```
B: NotCalculated → Calculating → (stays Calculating during cycle)
D: NotCalculated → Calculating → (stays Calculating during cycle)
E: NotCalculated → Calculating → (stays Calculating during cycle)
```

**After pass 1 completes (current behavior):**
```
B: Calculating → Calculated(T_B1) via record_value
D: Calculating → Calculated(T_D1) via record_value
```

**New behavior needed:**
```
Pass 1: Store to PreliminaryAnswers, stay in Calculating
Pass 2:
  - B (break_at): stays Calculating, T_B1 available in PreliminaryAnswers
  - D: stays Calculating, but tracked as pass2_participant so get_idx bypasses propose_calculation
  - E: stays Calculating, but tracked as pass2_participant so get_idx bypasses propose_calculation
After pass 2 completes:
  - B: Calculating → Calculated(T_B1 or T_B2) via record_value
  - D: Calculating → Calculated(T_D2) via record_value
  - E: Calculating → Calculated(T_E2) via record_value
```

**Key insight:** Calculations are NEVER reset. They stay in `Calculating` state throughout
cycle resolution. All intermediate results live in thread-local PreliminaryAnswers.
Only after pass 2 completes do we call record_value to finalize.

---

## Implementation Plan

### Step 1: Add SparseIndexMap for Preliminary Storage

Create `pyrefly_graph/src/sparse_index_map.rs`:

```rust
use std::marker::PhantomData;
use starlark_map::small_map::SmallMap;
use crate::index::Idx;

/// A sparse mapping from Idx<K> to V, using a SmallMap for efficiency when few entries.
///
/// This is similar to IndexMap but uses sparse storage (SmallMap) instead of dense storage (Vec).
/// It's designed for scenarios where only a small subset of indices will have values, such as
/// storing preliminary answers during cycle resolution.
#[derive(Debug, Clone)]
pub struct SparseIndexMap<K, V> {
    items: SmallMap<usize, V>,
    phantom: PhantomData<K>,
}

impl<K, V> Default for SparseIndexMap<K, V> {
    fn default() -> Self {
        Self::new()
    }
}

impl<K, V> SparseIndexMap<K, V> {
    pub fn new() -> Self {
        Self {
            items: SmallMap::new(),
            phantom: PhantomData,
        }
    }

    pub fn get(&self, idx: Idx<K>) -> Option<&V> {
        self.items.get(&idx.idx())
    }

    pub fn insert(&mut self, idx: Idx<K>, value: V) -> Option<V> {
        self.items.insert(idx.idx(), value)
    }

    pub fn clear(&mut self) {
        self.items.clear()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }
}
```

Export it from `pyrefly_graph/src/lib.rs`:

```rust
pub mod sparse_index_map;
```

### Step 2: Create TypeErasedPreliminaryAnswers

Add to `pyrefly/lib/alt/answers_solver.rs`:

```rust
use std::any::Any;
use std::any::TypeId;

/// Key for type-erased preliminary answer storage.
/// Uniquely identifies an answer by module, key type, and index.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct TypeErasedKey {
    module_name: ModuleName,
    key_type: TypeId,
    index: usize,
}

/// Type-erased storage for preliminary answers and their associated errors.
///
/// This stores answers as `Arc<dyn Any + Send + Sync>` to avoid cascading trait
/// bounds when recording preliminary answers from generic contexts.
/// Errors are stored separately keyed by CalcId.
#[derive(Debug, Default)]
pub struct TypeErasedPreliminaryAnswers {
    answers: RefCell<SmallMap<TypeErasedKey, Arc<dyn Any + Send + Sync>>>,
    errors: RefCell<SmallMap<CalcId, ErrorCollector>>,
}

impl Clone for TypeErasedPreliminaryAnswers {
    fn clone(&self) -> Self {
        Self {
            answers: RefCell::new(self.answers.borrow().clone()),
            errors: RefCell::new(SmallMap::new()), // Errors are not cloned
        }
    }
}

impl TypeErasedPreliminaryAnswers {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record an answer in type-erased storage.
    pub fn record<K: Keyed>(&self, module: &ModuleInfo, idx: Idx<K>, answer: Arc<K::Answer>)
    where
        K::Answer: Any + Send + Sync,
    {
        let key = TypeErasedKey {
            module_name: module.name().clone(),
            key_type: TypeId::of::<K>(),
            index: idx.idx(),
        };
        self.answers.borrow_mut().insert(key, answer);
    }

    /// Get an answer from type-erased storage, downcasting to the expected type.
    pub fn get<K: Keyed>(&self, module: &ModuleInfo, idx: Idx<K>) -> Option<Arc<K::Answer>>
    where
        K::Answer: Any + Send + Sync + Clone,
    {
        let key = TypeErasedKey {
            module_name: module.name().clone(),
            key_type: TypeId::of::<K>(),
            index: idx.idx(),
        };
        let borrow = self.answers.borrow();
        let any_arc = borrow.get(&key)?;
        // Downcast from Arc<dyn Any> to Arc<K::Answer>
        any_arc
            .downcast_ref::<K::Answer>()
            .map(|answer| Arc::new(answer.clone()))
    }

    /// Record errors associated with a CalcId.
    pub fn record_errors(&self, calc_id: &CalcId, errors: ErrorCollector) {
        self.errors.borrow_mut().insert(calc_id.clone(), errors);
    }

    /// Get errors associated with a CalcId.
    pub fn get_errors(&self, calc_id: &CalcId) -> Option<ErrorCollector> {
        self.errors.borrow_mut().remove(calc_id)
    }

    pub fn is_empty(&self) -> bool {
        self.answers.borrow().is_empty()
    }

    pub fn clear(&self) {
        self.answers.borrow_mut().clear();
        self.errors.borrow_mut().clear();
    }

    pub fn len(&self) -> usize {
        self.answers.borrow().len()
    }
}
```

**Important:** This requires adding bounds to `Keyed::Answer` in `binding.rs`:

```rust
pub trait Keyed: Hash + Eq + Clone + DisplayWith<ModuleInfo> + Debug + Ranged + 'static {
    const EXPORTED: bool = false;
    type Value: Debug + DisplayWith<Bindings>;
    /// Answer type must be thread-safe for type-erased storage during cycle resolution.
    type Answer: Clone + Debug + Display + TypeEq + VisitMut<Type> + Send + Sync + 'static;
    fn to_anyidx(idx: Idx<Self>) -> AnyIdx;
}
```

### Step 3: Add PreliminaryAnswers to Cycle

Modify the `Cycle` struct:

```rust
pub struct Cycle {
    break_at: CalcId,
    recursion_stack: Vec<CalcId>,
    unwind_stack: Vec<CalcId>,
    unwound: Vec<CalcId>,
    detected_at: CalcId,
    /// Type-erased storage for answers computed during pass 1.
    pub preliminary_answers: TypeErasedPreliminaryAnswers,
}
```

Update `Cycle::new()` to initialize it:

```rust
preliminary_answers: TypeErasedPreliminaryAnswers::new(),
```

### Step 4: Add Cycle Query Methods

Add to the `Cycles` impl:

```rust
impl Cycles {
    /// Check if we're currently in an active cycle (and NOT in pass 2).
    /// Used to determine whether to write to preliminary_answers vs global.
    pub fn is_in_active_cycle(&self, in_pass2: bool) -> bool {
        !in_pass2 && !self.0.borrow().is_empty()
    }

    /// Look up a preliminary answer from active cycles' storage.
    /// Checks all active cycles from innermost to outermost.
    pub fn get_preliminary<K: Keyed>(
        &self,
        module: &ModuleInfo,
        idx: Idx<K>,
    ) -> Option<Arc<K::Answer>>
    where
        K::Answer: Any + Send + Sync + Clone,
    {
        // Check cycles from innermost (most recent) to outermost
        for cycle in self.0.borrow().iter().rev() {
            if let Some(answer) = cycle.preliminary_answers.get::<K>(module, idx) {
                return Some(answer);
            }
        }
        None
    }

    /// Record a preliminary answer in the active (innermost) cycle's storage.
    pub fn record_preliminary<K: Keyed>(
        &self,
        module: &ModuleInfo,
        idx: Idx<K>,
        answer: Arc<K::Answer>,
    ) where
        K::Answer: Any + Send + Sync,
    {
        if let Some(cycle) = self.0.borrow().last() {
            cycle.preliminary_answers.record(module, idx, answer);
        }
    }

    /// Record errors associated with a CalcId in the active cycle's storage.
    pub fn record_preliminary_errors(&self, calc_id: &CalcId, errors: ErrorCollector) {
        if let Some(cycle) = self.0.borrow().last() {
            cycle.preliminary_answers.record_errors(calc_id, errors);
        }
    }

    /// Get errors associated with a CalcId from the active cycles' storage.
    pub fn get_preliminary_errors(&self, calc_id: &CalcId) -> Option<ErrorCollector> {
        for cycle in self.0.borrow().iter().rev() {
            if let Some(errors) = cycle.preliminary_answers.get_errors(calc_id) {
                return Some(errors);
            }
        }
        None
    }

    /// Clear preliminary storage for a completed cycle.
    pub fn clear_preliminary_for_cycle(&self, cycle_info: &CompletedCycleInfo) {
        // The cycle has already been popped from the stack, but we may still have
        // a reference to clear. In practice, we just need to ensure the storage
        // doesn't persist after the cycle is done.
        // This is mostly a placeholder - actual cleanup happens when cycle is popped.
    }
}
```

### Step 5: Add ThreadState Pass 2 Tracking

Modify `ThreadState` to track both the pass 2 flag and the set of participants being recomputed:

```rust
use std::collections::HashSet;

pub struct ThreadState {
    cycles: Cycles,
    stack: CalcStack,
    debug: RefCell<bool>,
    /// Track whether we're executing pass 2 recomputation.
    in_pass2: RefCell<bool>,
    /// Track which bindings are pass 2 participants (need to bypass propose_calculation).
    pass2_participants: RefCell<HashSet<CalcId>>,
}

impl ThreadState {
    pub fn new() -> Self {
        Self {
            cycles: Cycles::new(),
            stack: CalcStack::new(),
            debug: RefCell::new(false),
            in_pass2: RefCell::new(false),
            pass2_participants: RefCell::new(HashSet::new()),
        }
    }

    pub fn is_in_pass2(&self) -> bool {
        *self.in_pass2.borrow()
    }

    pub fn set_in_pass2(&self, value: bool) -> bool {
        let prev = *self.in_pass2.borrow();
        *self.in_pass2.borrow_mut() = value;
        prev
    }

    /// Check if the given CalcId is a pass 2 participant (needs to bypass propose_calculation).
    pub fn is_pass2_participant(&self, calc_id: &CalcId) -> bool {
        self.pass2_participants.borrow().contains(calc_id)
    }

    /// Register bindings as pass 2 participants.
    pub fn set_pass2_participants(&self, participants: impl IntoIterator<Item = CalcId>) {
        let mut set = self.pass2_participants.borrow_mut();
        set.clear();
        set.extend(participants);
    }

    /// Clear pass 2 participants after cycle resolution completes.
    pub fn clear_pass2_participants(&self) {
        self.pass2_participants.borrow_mut().clear();
    }
}
```

### Step 6: Modify get_idx for Lookup Cascade and Pass 2 Bypass

Update `get_idx` to check preliminary answers first and handle pass 2 participants:

```rust
pub fn get_idx<K: Solve<Ans>>(&self, idx: Idx<K>) -> Arc<K::Answer>
where
    AnswerTable: TableKeyed<K, Value = AnswerEntry<K>>,
    BindingTable: TableKeyed<K, Value = BindingEntry<K>>,
    K::Answer: Any + Send + Sync + Clone,  // Additional bound for type-erased storage
{
    let current = CalcId(self.bindings().dupe(), K::to_anyidx(idx));

    // Check preliminary answers first (from active cycles).
    // This ensures cycle participants see tentative answers during pass 1 and pass 2.
    if let Some(result) = self.cycles().get_preliminary(self.module(), idx) {
        return result;
    }

    // Pass 2 participant bypass: skip propose_calculation entirely.
    // These bindings are still in Calculating state from pass 1, but we need to recompute.
    if self.thread_state.is_pass2_participant(&current) {
        return self.calculate_for_pass2(current, idx);
    }

    let calculation = self.get_calculation(idx);
    // ... rest of existing implementation
}
```

### Step 7: Modify calculate_and_record_answer for Pass 1

Update `calculate_and_record_answer` to write to preliminary storage during pass 1:

```rust
fn calculate_and_record_answer<K: Solve<Ans>>(
    &self,
    current: CalcId,
    idx: Idx<K>,
    calculation: &Calculation<Arc<K::Answer>, Var>,
) -> Arc<K::Answer>
where
    AnswerTable: TableKeyed<K, Value = AnswerEntry<K>>,
    BindingTable: TableKeyed<K, Value = BindingEntry<K>>,
    K::Answer: Any + Send + Sync + Clone,
{
    let binding = self.bindings().get(idx);

    // Check if we're in an active cycle (pass 1) vs normal computation or pass 2.
    let in_active_cycle = self.cycles().is_in_active_cycle(self.thread_state.is_in_pass2());

    if in_active_cycle {
        // Pass 1: Buffer errors (they will be discarded), store to preliminary only.
        // DO NOT call record_value - the Calculation stays in Calculating state.
        let discard_errors = ErrorCollector::new(self.module().dupe(), ErrorStyle::Never);
        let answer = Arc::new(K::solve(self, binding, &discard_errors));

        // Store to preliminary answers (not global Calculation).
        self.cycles().record_preliminary(self.module(), idx, answer.clone());

        // Handle cycle unwinding (but no pass 2 trigger from here).
        self.cycles().on_calculation_finished(&current);

        return answer;
    }

    // Normal path (or pass 2): Use transactional errors, write to global.
    let local_errors = self.error_collector();
    let (answer, did_write) = calculation
        .record_value(K::solve(self, binding, &local_errors), |var, answer| {
            self.finalize_recursive_answer::<K>(idx, var, answer, &local_errors)
        });
    if did_write {
        self.base_errors.extend(local_errors);
    }

    // Handle cycle unwinding, which may trigger pass 2.
    let (_, completed_cycles) = self.cycles().on_calculation_finished(&current);

    // Pass 2 trigger: If any cycles completed where we were break_at, do recomputation.
    if !self.thread_state.is_in_pass2() {
        for cycle_info in completed_cycles {
            if cycle_info.break_at == current {
                self.execute_pass2_recomputation(&cycle_info, idx, &answer);
            }
        }
    }

    answer
}
```

### Step 8: Implement Cycle Completion and Pass 2

Add `CompletedCycleInfo` struct:

```rust
/// Information about a completed cycle, used to trigger pass 2 recomputation.
#[derive(Debug)]
pub struct CompletedCycleInfo {
    pub break_at: CalcId,
    /// All cycle participants (everything in unwound, including break_at).
    pub participants: Vec<CalcId>,
}
```

Modify `on_calculation_finished` to return completion info:

```rust
fn on_calculation_finished(&self, current: &CalcId) -> (bool, Vec<CompletedCycleInfo>) {
    let mut stack = self.0.borrow_mut();
    for cycle in stack.iter_mut() {
        cycle.on_calculation_finished(current);
    }

    let mut completed_cycles = Vec::new();
    while let Some(cycle) = stack.last_mut() {
        if cycle.unwind_stack.is_empty() {
            // Cycle complete! Collect all participants.
            let participants: Vec<CalcId> = cycle.unwound.iter().cloned().collect();

            completed_cycles.push(CompletedCycleInfo {
                break_at: cycle.break_at.dupe(),
                participants,
            });

            // NOTE: Do NOT clear preliminary_answers here - we need them for pass 2.
            // They will be cleared after pass 2 completes.
            stack.pop();
        } else {
            break;
        }
    }

    (!stack.is_empty(), completed_cycles)
}
```

Add the `calculate_for_pass2` method (used by get_idx for pass 2 participants):

```rust
/// Calculate a pass 2 participant without going through propose_calculation.
/// This bypasses cycle detection since we know this binding is being recomputed.
fn calculate_for_pass2<K: Solve<Ans>>(
    &self,
    current: CalcId,
    idx: Idx<K>,
) -> Arc<K::Answer>
where
    AnswerTable: TableKeyed<K, Value = AnswerEntry<K>>,
    BindingTable: TableKeyed<K, Value = BindingEntry<K>>,
    K::Answer: Any + Send + Sync + Clone,
{
    let binding = self.bindings().get(idx);

    // Use normal error collection - these errors will be committed.
    let local_errors = self.error_collector();
    let answer = Arc::new(K::solve(self, binding, &local_errors));

    // Store in PreliminaryAnswers (will be committed to Calculation after pass 2).
    self.cycles().record_preliminary(self.module(), idx, answer.clone());

    // Store errors with the answer for later commitment.
    self.cycles().record_preliminary_errors(&current, local_errors);

    answer
}
```

Add the pass 2 execution method:

```rust
/// Execute pass 2 recomputation for a completed cycle.
fn execute_pass2_recomputation<K: Solve<Ans>>(
    &self,
    cycle_info: &CompletedCycleInfo,
    break_at_idx: Idx<K>,
    t_b1: &Arc<K::Answer>,
) where
    AnswerTable: TableKeyed<K, Value = AnswerEntry<K>>,
    BindingTable: TableKeyed<K, Value = BindingEntry<K>>,
{
    tracing::debug!(
        "Pass 2 recomputation: {} participants for cycle at {:?}",
        cycle_info.participants.len(),
        cycle_info.break_at
    );

    // Register non-break_at participants for pass 2 bypass in get_idx.
    let pass2_participants: Vec<CalcId> = cycle_info
        .participants
        .iter()
        .filter(|c| **c != cycle_info.break_at)
        .cloned()
        .collect();
    self.thread_state.set_pass2_participants(pass2_participants);

    // Set in_pass2 flag.
    let prev_in_pass2 = self.thread_state.set_in_pass2(true);

    // Re-solve break_at to trigger recomputation of all participants.
    // break_at's T_B1 is still in PreliminaryAnswers, so dependencies will see it.
    let binding = self.bindings().get(break_at_idx);
    let local_errors = self.error_collector();
    let t_b2 = K::solve(self, binding, &local_errors);

    // Store break_at's pass 2 result and errors.
    let break_at_current = CalcId(self.bindings().dupe(), K::to_anyidx(break_at_idx));
    self.cycles().record_preliminary(self.module(), break_at_idx, Arc::new(t_b2.clone()));
    self.cycles().record_preliminary_errors(&break_at_current, local_errors);

    self.thread_state.set_in_pass2(prev_in_pass2);
    self.thread_state.clear_pass2_participants();

    // Now commit ALL participants to their Calculations.
    self.commit_cycle_results(&cycle_info);

    // Compare T_B1 vs T_B2 for stability monitoring.
    let t_b1_str = format!("{}", t_b1);
    let t_b2_str = format!("{}", t_b2);

    if t_b1_str != t_b2_str {
        tracing::warn!(
            "Cycle resolution unstable at {:?}: T_B1='{}' != T_B2='{}'",
            cycle_info.break_at, t_b1_str, t_b2_str
        );
    }
}
```

### Step 9: Commit Cycle Results

Add method to commit all cycle results after pass 2:

```rust
/// Commit all cycle participants' results from PreliminaryAnswers to Calculations.
/// This is called after pass 2 completes.
fn commit_cycle_results(&self, cycle_info: &CompletedCycleInfo) {
    for calc_id in &cycle_info.participants {
        // Get the answer and errors from PreliminaryAnswers.
        // Use type-erased dispatch to call record_value for each participant.
        self.commit_participant_result(calc_id);
    }

    // Clear the preliminary storage for this cycle.
    self.cycles().clear_preliminary_for_cycle(cycle_info);
}

/// Dispatch on AnyIdx to commit a participant's result to its Calculation.
fn commit_participant_result(&self, calc_id: &CalcId) {
    let bindings = &calc_id.0;
    let any_idx = &calc_id.1;

    // Only handle same-module commits. Cross-module participants are handled
    // by their own AnswersSolver when it processes the cycle.
    if bindings.module() != self.module() {
        return;
    }

    // Type-erased dispatch to commit each answer type.
    // The implementation retrieves the answer from PreliminaryAnswers,
    // calls record_value on the Calculation, and commits errors if successful.
    match any_idx {
        AnyIdx::Key(idx) => self.commit_typed_result::<Key>(*idx, calc_id),
        AnyIdx::KeyExpect(idx) => self.commit_typed_result::<KeyExpect>(*idx, calc_id),
        // ... similar for all other AnyIdx variants
    }
}

fn commit_typed_result<K: Solve<Ans>>(&self, idx: Idx<K>, calc_id: &CalcId)
where
    AnswerTable: TableKeyed<K, Value = AnswerEntry<K>>,
    BindingTable: TableKeyed<K, Value = BindingEntry<K>>,
    K::Answer: Any + Send + Sync + Clone,
{
    if let Some(answer) = self.cycles().get_preliminary::<K>(self.module(), idx) {
        let calculation = self.get_calculation(idx);
        let (_, did_write) = calculation.record_value(
            (*answer).clone(),
            |var, answer| self.finalize_recursive_answer::<K>(idx, var, Arc::new(answer), &self.error_swallower()).as_ref().clone()
        );
        if did_write {
            if let Some(errors) = self.cycles().get_preliminary_errors(calc_id) {
                self.base_errors.extend(errors);
            }
        }
    }
}
```

---

## Complete Flow Example

**Cycle:** A → B → A (break at A)

**Pass 1:**
```
1. get_idx(A) → propose_calculation → A: Calculating
2. K::solve(A) calls get_idx(B)
3. get_idx(B) → propose_calculation → B: Calculating
4. K::solve(B) calls get_idx(A)
5. get_idx(A) sees A is Calculating with same thread → CycleDetected
6. Store T_A0 (placeholder) in PreliminaryAnswers
7. Return T_A0 to B
8. B completes with T_B1 (uses T_A0)
9. is_in_active_cycle() = true → store T_B1 in PreliminaryAnswers, B stays Calculating
10. A completes with T_A1
11. is_in_active_cycle() = true → store T_A1 in PreliminaryAnswers, A stays Calculating
12. unwind_stack empties → cycle completes, CompletedCycleInfo returned
```

**Transition to Pass 2:**
```
13. T_A1 remains in PreliminaryAnswers (NOT cleared yet)
14. Register B as pass2_participant in ThreadState
15. set_in_pass2(true)
```

**Pass 2:**
```
16. K::solve(A) - calling directly, not via get_idx
17. K::solve(A) calls get_idx(B)
18. get_idx(B) → check PreliminaryAnswers (empty for B, T_B1 was for pass 1)
19. get_idx(B) → is_pass2_participant(B) = true → use calculate_for_pass2
20. calculate_for_pass2(B) calls K::solve(B)
21. K::solve(B) calls get_idx(A)
22. get_idx(A) → PreliminaryAnswers has T_A1 → return T_A1
23. B completes with T_B2 (uses T_A1 - the real type!)
24. Store T_B2 in PreliminaryAnswers, store B's errors
25. A completes with T_A2
26. Store T_A2 in PreliminaryAnswers, store A's errors
27. set_in_pass2(false), clear pass2_participants
```

**Commit Phase:**
```
28. commit_cycle_results() for all participants
29. For A: record_value(T_A2) → A: Calculated(T_A2), commit A's errors
30. For B: record_value(T_B2) → B: Calculated(T_B2), commit B's errors
31. Clear PreliminaryAnswers for this cycle
```

**Final state:**
```
A: Calculated(T_A2) - from pass 2
B: Calculated(T_B2) - from pass 2
Errors: All from pass 2 - computed with real types, no duplicates
```

**Key insight:** Calculations are NEVER reset. They stay in `Calculating` state
throughout pass 1 and pass 2. Only after pass 2 completes do we call record_value
to transition them to `Calculated` state.

---

## Error Handling Summary

| Scenario | Errors From | Committed? |
|----------|-------------|------------|
| Pass 1 (all participants) | Pass 1 computation | No (ErrorStyle::Never discards) |
| Pass 2 (all participants) | Pass 2 recomputation | Yes (stored, then committed in commit phase) |

**Key insight:** All errors come from pass 2, which computes with real types (T_B1)
instead of placeholders (T_B0). Each binding's errors are committed exactly once
during the commit phase after pass 2 completes.

---

## Files to Modify

1. **`pyrefly_graph/src/sparse_index_map.rs`** (new file)
   - `SparseIndexMap<K, V>` struct

2. **`pyrefly_graph/src/lib.rs`**
   - Export `sparse_index_map` module

3. **`pyrefly/lib/binding/binding.rs`**
   - Add `Send + Sync + 'static` bounds to `Keyed::Answer`

4. **`pyrefly/lib/alt/answers_solver.rs`**
   - Add `TypeErasedKey` struct
   - Add `TypeErasedPreliminaryAnswers` struct with error storage
   - Add `preliminary_answers` field to `Cycle`
   - Add `CompletedCycleInfo` struct
   - Add `is_in_active_cycle()`, `get_preliminary()`, `record_preliminary()` to `Cycles`
   - Add `record_preliminary_errors()`, `get_preliminary_errors()`, `clear_preliminary_for_cycle()` to `Cycles`
   - Add `in_pass2` flag and `pass2_participants` set to `ThreadState`
   - Add `is_pass2_participant()`, `set_pass2_participants()`, `clear_pass2_participants()` to `ThreadState`
   - Modify `get_idx` for lookup cascade and pass 2 bypass
   - Modify `calculate_and_record_answer` for pass 1 behavior
   - Add `calculate_for_pass2()` method
   - Modify `on_calculation_finished` to return `CompletedCycleInfo`
   - Add `execute_pass2_recomputation()`
   - Add `commit_cycle_results()`, `commit_participant_result()`, `commit_typed_result()`

---

## Testing Strategy

1. **Single-thread cycle test:** Verify T_B1 vs T_B2 comparison works
2. **Multi-thread cycle test:** Verify only one thread's errors committed
3. **Nested cycle test:** Verify inner cycles resolve before outer
4. **Cross-module cycle test:** Verify cycles spanning modules work

---

## Open Questions

### Q1: Thread safety with in_pass2 flag and pass2_participants?

Both the `in_pass2` flag and `pass2_participants` set are per-thread (in ThreadState via RefCell).
No races because each thread has its own ThreadState.

### Q2: What if pass 2 discovers different cycles?

If T_A1 changes control flow and pass 2 discovers cycle C2 instead of the original C1,
that's fine. C2 goes through the normal cycle resolution protocol. The final results
are based on whatever cycles pass 2 discovers.

---

## Implementation Order

1. **Step 1:** Add SparseIndexMap (new file, low risk)
2. **Step 2:** Add `Send + Sync + 'static` bounds to `Keyed::Answer`
3. **Step 3:** Add `TypeErasedPreliminaryAnswers` with error storage
4. **Step 4:** Add `preliminary_answers` to Cycle
5. **Step 5:** Add `in_pass2` flag and `pass2_participants` set to ThreadState
6. **Step 6:** Add `is_in_active_cycle()`, `get_preliminary()`, `record_preliminary()` to Cycles
7. **Step 7:** Modify `get_idx` for lookup cascade and pass 2 bypass
8. **Step 8:** Modify `calculate_and_record_answer` for pass 1 behavior
9. **Step 9:** Add `calculate_for_pass2()` method
10. **Step 10:** Modify `on_calculation_finished` to return CompletedCycleInfo
11. **Step 11:** Add `execute_pass2_recomputation()` method
12. **Step 12:** Add `commit_cycle_results()` and related commit methods
13. **Test:** Run test suite, verify no regressions

---

## Success Criteria

- All existing tests pass
- No duplicate errors for cycle bindings
- T_B1 vs T_B2 comparisons logged for monitoring
- Errors reference resolved types (T_B1), not placeholders (T_B0)
- Calculations are never reset - they transition NotCalculated → Calculating → Calculated exactly once
- All cycle resolution happens in thread-local PreliminaryAnswers before committing to Calculations
