# Cycle Resolution Analysis Notes

These notes capture analysis of Pyrefly's cycle resolution behavior, particularly
around nested/overlapping cycles. The goal is to understand potential issues
before implementing the two-pass cycle resolution (v2-doc).

---

## Example 1: Overlapping Cycles from D77669454

**Source:** D77669454 - "unwind all (applicable) cycles on calculation completed"

### Dependency Graph

```
2 → 8 → 4 → 6 → 9 → 8  (back-edge creates C1)
                ↓
                6      (additional edge 9→6 creates sub-cycle C2)
```

So the edges are:
- 2→8, 8→4, 4→6, 6→9, 9→8 (main chain + back-edge)
- 9→6 (discovered during unwind)

### Trace

**Phase 1: Initial computation**

| Step | Action | CalcStack | Cycles |
|------|--------|-----------|--------|
| 1 | get_idx(2) | [2] | [] |
| 2 | 2→8 | [2, 8] | [] |
| 3 | 8→4 | [2, 8, 4] | [] |
| 4 | 4→6 | [2, 8, 4, 6] | [] |
| 5 | 6→9 | [2, 8, 4, 6, 9] | [] |
| 6 | 9→8: CycleDetected | [2, 8, 4, 6, 9, 8] | [] |

At step 6:
- `current_cycle()` = [8, 9, 6, 4]
- `break_at` = 4 (minimum idx)
- Push C1 with `unwind_stack=[4,6,9,8]`, `recursion_stack=[]`

**Phase 2: Recursion to break_at**

| Step | Action | CalcStack | C1.unwind_stack |
|------|--------|-----------|-----------------|
| 7 | 8's solve needs 4, get_idx(4) | [2,8,4,6,9,8,4] | [4,6,9,8] |
| 8 | 4 is break_at → placeholder | [2,8,4,6,9,8] | [4,6,9,8] |
| 9 | 8 finishes, on_calc_finished | [2,8,4,6,9] | [4,6,9] |

**Phase 3: Sub-cycle discovery**

| Step | Action | CalcStack | Cycles |
|------|--------|-----------|--------|
| 10 | 9's solve needs 6, get_idx(6) | [2,8,4,6,9,6] | C1 |
| 11 | pre_calculate_state(6) → NoDetectedCycle | | |
| 12 | propose_calculation(6) → CycleDetected | | |
| 13 | current_cycle() = [6, 9] | | |
| 14 | break_at=6, BreakHere, push C2 | | C1, C2 |
| 15 | Placeholder for 6, return | [2,8,4,6,9] | C1, C2 |

**Phase 4: Shared unwind**

| Step | Action | C1.unwind | C2.unwind |
|------|--------|-----------|-----------|
| 16 | 9 finishes, on_calc_finished | [4,6] | [6] |
| 17 | Pop 9 from stack | | |
| 18 | 6 finishes, on_calc_finished | [4] | [] → pop C2 |
| 19 | 4 finishes, on_calc_finished | [] → pop C1 | |

### Observations

**The fishy behavior at step 11:**
- 6 IS in C1's `unwind_stack` [4,6,9]
- But `pre_calculate_state` only checks `break_at` and `recursion_stack.last()`
- Returns `NoDetectedCycle` even though 6 is an active cycle participant
- This leads to detecting C2 as a "new" cycle

**Is C2 a real cycle?**
YES - the dependency 6→9→6 is a real cycle in the graph. So C2 is not "garbage."

**What's fishy:**
- Node 6 now participates in TWO cycles (C1 and C2)
- `on_calculation_finished(6)` updates BOTH cycles' unwind_stacks
- The cycle "stack" doesn't map 1:1 to Rust call stack segments
- Reasoning about cycle state becomes harder

**Is this causing stack overflows?**
UNCLEAR. This example completes correctly. The overlapping cycles are handled,
just in a way that's harder to reason about.

**Alternative approach (not implemented):**
Instead of pushing C2, recognize that 6 is in C1 and compute it normally.
6's computation would hit 4's placeholder and stop. The cycles would then
form a proper stack.

---

## The `CycleBroken` Protection

When a cycle's break_at has a placeholder recorded (via `record_cycle`),
subsequent accesses return `CycleBroken(placeholder)` immediately:

```rust
// In propose_calculation:
Status::Calculating(calc) => {
    let (rec, threads) = &mut **calc;
    if threads.insert(thread::current().id()) {
        ProposalResult::Calculatable
    } else {
        match rec {
            None => ProposalResult::CycleDetected,
            Some(r) => ProposalResult::CycleBroken(r.dupe()),  // ← Returns immediately
        }
    }
}
```

This means: once we record a placeholder for break_at=A, ANY path that goes
through A will hit this and stop. This should prevent infinite recursion
"through" an enclosing cycle.

**Open question:** Is this protection sufficient, or are there scenarios where
we can infinite-loop without ever hitting a break_at?

---

## Draft Change 84b4fa5449

This change adds `EnclosingCycleParticipant` detection:
- When accessing a binding B from an inner cycle
- If B is in an OUTER cycle's in-progress set (break_at, recursion_stack, or unwind_stack)
- Return `EnclosingCycleParticipant` instead of `NoDetectedCycle`

**The proposed handling in 84b4fa5449:**
Return a placeholder immediately for B.

**Problem with this approach:**
Only break_at bindings should get placeholders. Returning placeholder for
non-break_at bindings is semantically incorrect.

**Alternative approach (discussed but not implemented):**
Compute B normally. B's computation will eventually hit the enclosing cycle's
break_at placeholder and stop. No need for an extra placeholder.

---

## Counter-Based Cycle Detection (Proposed)

To distinguish "re-entering enclosing cycle" from "new cycle":

**Idea:** Track how many times a binding can legitimately appear on the stack.

- Base: 1 (initial computation)
- Add 1 for each cycle the binding participates in

When occurrences > allowed, it's a new cycle.

**Complication:** The count needs to be per-thread and updated as cycles are
pushed/popped.

---

## Stack Overflow Analysis (from production traces)

**Sources:** P2108270570, P2108272595, P2108274049, P2108279227

**Important context:** These stack traces come from BEFORE duplicate cycle detection
was added to the codebase. The duplicate detection (`DuplicateCycleDetected` at
`MAXIMUM_CYCLE_DEPTH=100`) was added as a safety net after these crashes were observed.

**The key question we're investigating:** Can duplicate cycles actually occur on trunk
today? If so, the duplicate detection is actively preventing stack overflows, and the
underlying issue still exists. If duplicates can't occur, then either:
1. Some other fix resolved the root cause, or
2. The original analysis of what caused these crashes was incorrect

### Key Findings

**Two distinct patterns observed:**

1. **Mutual recursion pattern** (Pastes 1 & 4):
   - `KeyClassSynthesizedFields` ↔ `KeyAnnotation` alternating
   - Approximately 65-69 frames between each pair

2. **Self-loop pattern** (Pastes 2 & 3):
   - Only `KeyClassSynthesizedFields` appearing repeatedly

### Paste 1 (P2108270570) Detailed Analysis

**Key type distribution:**
- KeyClassSynthesizedFields: 440 occurrences
- KeyDecorator: 220 occurrences
- KeyAnnotation: 216 occurrences

**The repeating dependency chain:**
```
Frame 55: get_idx<KeyClassSynthesizedFields>
Frame 54: calculate_and_record_answer<KeyDecorator>
Frame 53: as_type_alias
Frame 52: force_for_narrowing
Frame 51: solve_tparams
Frame 50: get_class_tparams
Frame 49: get_idx<KeyAnnotation>
Frame 48: finalize_recursive_answer<KeyClassSynthesizedFields>
Frame 47: solve_yield_from
Frame 46: calculate_class_field
... (expression evaluation) ...
→ back to get_idx<KeyClassSynthesizedFields>
```

**Critical observation:**
This is NOT a cycle of a single binding being re-computed. Each `get_idx` call
has the same function address but operates on DIFFERENT Idx values (different
bindings). This represents an **unbounded chain of distinct dependencies**:

```
ClassA.synthesized_fields → ClassA.decorator → ClassB.annotation →
ClassB.synthesized_fields → ClassB.decorator → ClassC.annotation → ...
```

### Why CycleBroken Doesn't Help

The `CycleBroken` protection only works for TRUE cycles (same binding appearing
twice on the stack). When we have an unbounded chain of DIFFERENT bindings:
- A.fields → B.annotation → B.fields → C.annotation → C.fields → ...

Each binding is distinct, so `propose_calculation` returns `Calculatable` for
each one. The thread ID check passes because it's a new binding we haven't
seen on this thread's stack before.

### Root Cause Hypothesis

The stack overflow is caused by **unbounded transitive dependencies** through
class hierarchies or type alias chains. Possible scenarios:

1. **Recursive type aliases**: `type A = B[C]; type B = A[D]` where the
   parameterization creates new synthesized fields each time

2. **Metaclass chains**: Complex metaclass relationships creating unbounded
   decorator → annotation → synthesized_fields dependencies

3. **Cyclic generic instantiation**: `class A(Generic[T]): pass` where
   instantiation chains like `A[A[A[...]]]` occur

### Implications for Two-Pass Resolution

The two-pass cycle resolution (v2-doc) won't fix this issue because:
1. It's not a cycle detection problem
2. It's an unbounded dependency chain problem
3. Need a separate mechanism to detect and limit depth of dependency chains

### Potential Fixes

1. **Depth limit**: Add a configurable maximum recursion depth for get_idx
   (e.g., 1000 calls) and return error type when exceeded

2. **Memoization**: Ensure intermediate results are cached to prevent
   recomputation of the same types

3. **Cycle detection across bindings**: Track the TYPES being computed,
   not just binding Idx values, to detect when we're computing equivalent
   types repeatedly

4. **Lazier evaluation**: Defer some computations (like class field synthesis)
   to avoid eager evaluation chains

---

## Root Cause Identified: `pre_calculate_state` Bug

**Location:** `answers_solver.rs:274-286`

### The Bug

```rust
fn pre_calculate_state(&mut self, current: &CalcId) -> CycleState {
    if *current == self.break_at {
        CycleState::BreakAt
    } else if let Some(c) = self.recursion_stack.last()
        && *current == *c
    {
        // Correct: checks last item of recursion_stack
        CycleState::Participant
    } else {
        CycleState::NoDetectedCycle  // Bug: doesn't check unwind_stack!
    }
}
```

**The recursion_stack.last() check is correct** because during the recursion phase,
the computation path is deterministic. We encounter recursion_stack elements in
exactly LIFO order as we retrace the dependency path toward break_at.

**The bug is that unwind_stack is never checked.** During the unwind phase:
- Computation completes with placeholder/preliminary values
- Finalization code (force_var, record_recursive, etc.) can access ANY binding
- This is fundamentally unpredictable - not retracing the original path

### How This Causes Stack Overflow (Unwind Phase)

1. Cycle C1 detected with [A, B, C, D], break_at=A
2. After recursion completes, unwind_stack=[A, B, C, D]
3. D finishes, C finishes, now B is finishing
4. B's finalization code reads C (still in unwind_stack)
5. `pre_calculate_state(C)`:
   - C != break_at (A) ✗
   - C != recursion_stack.last() (empty) ✗
   - **Doesn't check unwind_stack!**
   - Returns NoDetectedCycle
6. `propose_calculation(C)` returns CycleDetected (thread already computing C)
7. NEW cycle C2 created with C
8. C now participates in BOTH C1 and C2
9. Repeats, creating unbounded overlapping cycles

### Why Duplicate Detection Doesn't Help

- `DuplicateCycleDetected` only triggers when:
  1. cycles.len() > 100 (MAXIMUM_CYCLE_DEPTH)
  2. AND `participants_normalized()` exactly matches an existing cycle

- With overlapping cycles that have different "shapes" (different entry points,
  different subsets of participants), they never match even though they share
  bindings.

### The Fix

Add an `unwind_stack` check to `pre_calculate_state`:

```rust
fn pre_calculate_state(&mut self, current: &CalcId) -> CycleState {
    if *current == self.break_at {
        CycleState::BreakAt
    } else if let Some(c) = self.recursion_stack.last()
        && *current == *c
    {
        let c = self.recursion_stack.pop().unwrap();
        self.unwind_stack.push(c);
        CycleState::Participant
    } else if self.unwind_stack.contains(current) {
        CycleState::Participant  // The fix: check unwind_stack
    } else {
        CycleState::NoDetectedCycle
    }
}
```

This ensures that bindings accessed during the unwind phase are recognized as
cycle participants, preventing new overlapping cycles from being created.

Note: Checking the entire `recursion_stack` (not just `.last()`) is unnecessary
because the recursion phase is deterministic - we encounter elements in LIFO order.

---

## Updated Hypothesis

The stack overflows are NOT from unbounded chains of distinct bindings.
They're from the SAME bindings being re-encountered through different paths,
creating overlapping cycles that bypass the duplicate detection.

The extremely regular 65-frame pattern supports this: the same computation
is happening repeatedly for the same bindings, not for distinct classes.

---

## Open Questions

1. ~~Can we construct a scenario where infinite recursion actually occurs
   (not prevented by `CycleBroken`)?~~
   **ANSWERED**: Yes - the `pre_calculate_state` bug allows bindings in
   `recursion_stack` (not last) or `unwind_stack` to trigger new cycles.

2. ~~Is the overlapping-cycles behavior in Example 1 related to stack overflow
   reports, or is it a separate issue?~~
   **ANSWERED**: They are the SAME issue. The overlapping cycles from Example 1
   are exactly what causes the stack overflows.

3. Would enforcing proper cycle stacking (each cycle maps to distinct call
   stack segment) simplify the implementation and prevent edge cases?
   **LIKELY YES** - fixing `pre_calculate_state` would prevent overlapping cycles.

4. **NEW**: What Python code patterns produce these unbounded dependency chains?
   Need to find/reproduce the actual source code that triggers P2108270570 etc.

5. ~~Should we add depth limiting as a stopgap before fixing the
   underlying evaluation order issues?~~
   **NO** - fix the root cause in `pre_calculate_state` instead.

---

## True Infinite Recursion vs Quadratic Stack Usage

**Key clarification:** `break_at` is deterministic - it's always the minimum Idx among
cycle participants. For any given set of participants, break_at is always the same.

### Does break_at Alone Prevent Infinite Recursion?

**YES** - break_at prevents TRUE infinite recursion (unbounded, never-terminating).

**Reasoning:**
1. When a cycle is detected, break_at = min(participants)
2. A placeholder is recorded at break_at
3. Any subsequent access to break_at returns CycleBroken(placeholder)
4. This "seals" that binding - no cycle including it can form again

However, overlapping cycles with DIFFERENT participants can still form:
1. C1 forms with {A, B, C, D}, break_at = A (assuming A is min)
2. A gets placeholder (sealed)
3. During C1's unwind, accessing E creates C2 with {B, C, E}, break_at = B
4. B gets placeholder (sealed)
5. During C2's unwind, accessing F creates C3 with {C, E, F}, break_at = C
6. C gets placeholder (sealed)
7. ...continues until all relevant bindings are sealed

### Stack Usage Analysis

With N bindings:
- Maximum number of nested cycles: O(N) (each cycle seals at least one binding)
- Stack frames per cycle: O(N) (each cycle can involve up to N dependencies)
- **Total stack usage: O(N²)**

For programs with thousands of bindings, O(N²) stack frames easily exceeds the
default ~8MB stack limit.

### Explaining the Repeating 65-Frame Patterns

The stack traces show identical repeating patterns because:
1. Each nested cycle involves similar dependency chains
   (synthesized_fields → decorator → annotation → synthesized_fields)
2. These produce identical Rust function call patterns
3. With 100+ nested cycles, the same ~65 frame pattern appears repeatedly

**This is bounded quadratic behavior, not true infinite recursion** - but the
practical effect (stack overflow) is the same.

### Conclusion

- **break_at alone**: Guarantees termination (bounded by O(N²) stack usage)
- **duplicate detection**: Catches cycles with similar structure, potentially
  reducing depth from O(N²) to O(N) in some cases
- **unwind_stack fix**: Would prevent most overlapping cycles, reducing the
  practical depth significantly

The stack overflows are real, but they represent **very deep bounded recursion**
rather than **truly infinite recursion**.

---

## CORRECTED: `current_cycle()` Analysis

**Location:** `answers_solver.rs` in `CalcStack::current_cycle()`

### The Actual Code

```rust
pub fn current_cycle(&self) -> Option<Vec1<CalcId>> {
    let stack = self.0.borrow();
    let mut rev_stack = stack.iter().rev();  // REVERSE iteration
    let current = rev_stack.next()?;          // Get last element (current)
    let mut cycle = Vec1::with_capacity(current.dupe(), rev_stack.len());
    for c in rev_stack {                      // Walk BACKWARDS
        if c == current {                     // Stop at FIRST match found
            return Some(cycle);               // (which IS the nearest occurrence)
        }
        cycle.push(c.dupe());
    }
    None
}
```

### CORRECTION: Earlier Analysis Was Wrong

An earlier version of this document incorrectly claimed that `current_cycle()`
finds the FIRST occurrence. **This was wrong.**

The code iterates in REVERSE order, starting from the current element and walking
backwards. It stops at the first match it finds - which IS the nearest occurrence.

**Example:** CalcStack = `[E, A, Z, A, E, A]` (positions 0-5)
- `current` = A (position 5)
- Walking backwards: E (pos 4), A (pos 3) ← **STOP here!**
- Returns cycle `[A, E]` representing A→E→A (positions 3-5)
- Does NOT return `[A, Z, A, E, A]` (positions 1-5)

So `current_cycle()` correctly finds the **smallest/nearest** cycle.

---

## Multi-Cycle Pop: Necessary for Correctness

**Location:** `CycleStack::on_calculation_finished` in `answers_solver.rs:406-414`

```rust
fn on_calculation_finished(&self, current: &CalcId) -> bool {
    let mut stack = self.0.borrow_mut();
    for cycle in stack.iter_mut() {           // Iterates ALL cycles
        cycle.on_calculation_finished(current);
    }
    // ...
}
```

### Why Multi-Cycle Pop Is Necessary

When a cycle is detected with `break_at ≠ detected_at`, we get `Continue`. This
means the current Rust stack frame keeps executing while being added to a new
cycle's unwind_stack.

But this same stack frame may ALREADY be part of an earlier cycle's unwind_stack.
The same computation participates in multiple overlapping cycles simultaneously.

**Example:** CalcStack `[E, A, Z, A, E, A]`, detecting cycle `[A, E]`:
- If break_at ≠ detected_at → Continue
- The frame computing A (at position 5) continues executing
- This frame was already in Cycle1's unwind_stack (from the Z→A cycle)
- Now it's also in Cycle2's unwind_stack

When this A computation finishes, it must be popped from BOTH cycles. The
multi-cycle pop is **necessary for correctness**, not a hack or workaround.

### Implication

Overlapping cycles are **expected behavior** given how cycle detection works.
The multi-cycle pop correctly handles this. The question of what causes stack
overflow is separate from this mechanism.

---

## Remaining Mystery: Root Cause of Stack Overflow

We still don't have a clear explanation for what caused the stack overflows
observed in production (P2108270570, P2108272595, P2108274049, P2108279227).

**What we know:**
1. Stack overflows occurred before duplicate detection was added
2. Traces show repeating patterns through KeyClassSynthesizedFields → KeyDecorator → KeyAnnotation
3. Multiple different stack trace patterns were observed
4. The multi-cycle pop is necessary for correctness (not compensating for a bug)

**Hypotheses that were ruled out:**
- ~~`current_cycle()` using first occurrence instead of nearest~~ (code actually uses nearest)

**Remaining hypotheses:**
1. **`pre_calculate_state` bug**: Doesn't check `unwind_stack`, only `break_at` and
   `recursion_stack.last()`. During unwind phase, accessing bindings in unwind_stack
   returns `NoDetectedCycle`, then `propose_calculation` returns `CycleDetected`,
   creating overlapping cycles.

2. **O(N²) bounded growth**: With N bindings, we could have O(N) nested cycles,
   each with O(N) stack frames. For large codebases, O(N²) stack usage could
   exceed the default ~8MB stack limit even without infinite recursion.

3. **Specific code patterns**: Some Python patterns might create unusually deep
   cycle nesting that triggers the issue more readily.

**To investigate further:**
- Add instrumentation to trace cycle behavior in pyrefly
- Find actual repro cases from production logs
- Analyze whether the `pre_calculate_state` fix would prevent the issue

---

## O(N²) Complexity Example

Here's a concrete graph structure that demonstrates worst-case O(N²) stack usage
due to the `pre_calculate_state` bug.

### Graph Structure (N=5)

Nodes A, B, C, D, E in lexicographic (Idx) order. Each node depends on only the
next node, except the last which depends on all previous nodes:

```
A → B
B → C
C → D
D → E
E → A, B, C, D  (lookups in order)
```

### Trace

1. Computing E, CalcStack grows: `[E, A, B, C, D, E]`
2. **Cycle1** detected: `[E, A, B, C, D]` (5 nodes), break_at = A
3. Continue (A ≠ E), E's computation continues with A's placeholder
4. E accesses B - B is in Cycle1 but not recognized by `pre_calculate_state`
   (B is not break_at, not recursion_stack.last())
5. **Cycle2** detected: `[B, C, D, E]` (4 nodes), break_at = B
6. E accesses C - same issue
7. **Cycle3** detected: `[C, D, E]` (3 nodes), break_at = C
8. E accesses D - same issue
9. **Cycle4** detected: `[D, E]` (2 nodes), break_at = D

### Stack Frame Count

Total frames: 5 + 4 + 3 + 2 = 14 ≈ N²/2

For N nodes in this pattern, we get approximately N²/2 stack frames. With a
component of hundreds of bindings, this easily exceeds typical stack limits.

### Caveats and Reasons for Doubt

**This O(N²) analysis may be incorrect or irrelevant:**

1. **Doesn't match actual tracebacks**: Tracing through the example more carefully,
   after the first `Continue`, subsequent accesses to B, C, D each trigger cycles
   where `detected_at == break_at` (because the detected element IS the minimum
   of the remaining cycle). This gives `BreakHere`, not `Continue`, so we don't
   get nested recursive calls. The repeating frame patterns in production traces
   don't obviously match this structure.

2. **Real-world probability**: Even if O(N²) is possible in theory, it requires
   both long cycles AND worst-case ordering (the "hub" node that depends on all
   others must be processed last). If real-world dependency graphs resemble random
   graphs, the probability of hitting worst-case ordering likely drops toward zero
   as the graph grows. We have no evidence that real codebases produce this pattern.

3. **BreakHere dominates**: In practice, many cycle detections result in `BreakHere`
   (when `detected_at == break_at`), which records a placeholder immediately without
   adding stack frames. The conditions for `Continue` (detected_at ≠ break_at) may
   be rarer than this analysis assumes.

**Conclusion**: We have a plausible theoretical explanation for how O(N²) stack
usage could occur, but significant doubt remains about whether this mechanism
explains the actual production crashes. Further investigation is needed.