# Stack Unwinding for Cycle Resolution: Design Notes

This document describes a proposed approach to cycle handling in Pyrefly that uses
stack unwinding via `Result` types to enable cleaner cycle detection and resolution,
ultimately supporting fixpoint iteration over dynamically-discovered strongly-connected
components (SCCs).

**Companion documents (earlier explorations, now superseded):**
- `../thread-local-cycles/v1-doc.md` - Original design for thread-local cycle isolation
- `../thread-local-cycles/v2-doc.md` - Refined two-pass protocol with preliminary answers
- `../thread-local-cycles/v2-worked-example.md` - Detailed trace of cycle resolution

---

## Motivation

### Current Limitations

The current cycle handling in Pyrefly is tightly coupled to the Rust call stack:

1. **`get_idx` returns `Arc<K::Answer>` synchronously**: There's no way to "unwind"
   when a cycle is detected. Instead, cycle handling must be linear, duplicating
   computations in-place until a break point is reached.

2. **Interlocking cycles are problematic**: When multiple cycles share nodes, the
   order in which cycles are discovered can affect the final answer, leading to
   nondeterminism.

3. **Fixpoint iteration is impractical**: True fixpoint iteration requires the ability
   to restart computations with updated information. Without stack unwinding, partial
   state is scattered throughout the call stack and difficult to invalidate.

### Why Stack Unwinding Helps

With stack unwinding via `Result` types:

1. **Clean abort**: When a cycle is detected, we can cleanly unwind the stack back to
   a designated restart point, discarding partial computations.

2. **Deferred commitment**: Instead of committing to answers mid-computation, we can
   discover the full scope of a cycle (or SCC) before committing.

3. **Fixpoint enablement**: We can iterate over an SCC, unwinding and restarting when
   types change, until convergence.

---

## Phase 1: Type Rewiring

### Goal

Change `get_idx` to return a `Result`-like type, without changing any behavior. This
is purely mechanical preparation for later phases.

### The `Unwindable` Type

```rust
/// Represents a computation that may need to unwind due to cycle detection.
pub type Unwindable<T> = Result<T, CycleUnwind>;

/// Information about a cycle that requires unwinding.
///
/// This enum covers all cycle-related unwind scenarios:
/// - NewCycle: First discovery of a cycle (not connected to any existing SCC)
/// - ConnectionToExistingScc: Discovery of a connection to an SCC on the stack
/// - Restart: Instruction to restart fixpoint after SCC merge
pub enum CycleUnwind {
    /// A new cycle was detected that's not connected to any existing SCC.
    /// The break_at is the minimal idx in the cycle.
    NewCycle {
        break_at: CalcId,
        cycle_nodes: Vec<CalcId>,
    },

    /// A connection to an existing SCC on the stack was discovered.
    /// This triggers SCC merging.
    ConnectionToExistingScc {
        target_scc_depth: usize,
        node: CalcId,
    },

    /// Instruction to restart fixpoint from the anchor of a merged SCC.
    /// Returned after merging SCCs to unwind the stack.
    Restart {
        break_at: CalcId,
    },
}
```

**Note:** In Phase 1, only `NewCycle` is used (and immediately caught). The other
variants become relevant in Phase 2 and Phase 3.

### Signature Change

```rust
// Current
pub fn get_idx<K: Solve<Ans>>(&self, idx: Idx<K>) -> Arc<K::Answer>

// Phase 1
pub fn get_idx<K: Solve<Ans>>(&self, idx: Idx<K>) -> Unwindable<Arc<K::Answer>>
```

### Scope of Changes

Based on codebase analysis:

| Metric | Count |
|--------|-------|
| Direct `get_idx` call sites | ~119 |
| Files with direct calls | 22 |
| Files with transitive impact | ~30-40 |

The changes are mechanical:
- Add `?` to propagate `Unwindable` through call chains
- Wrap successful returns in `Ok(...)`
- Update `Solve::solve()` trait to return `Unwindable<Arc<Self::Answer>>`

### Phase 1 Invariant

During phase 1, `get_idx` always returns `Ok(...)`. The `Err(CycleUnwind)` case is
never produced. This ensures no behavioral changes while the types are wired up.

---

## Phase 2: Simple Stack Unwinding

### Goal

Use the unwinding mechanism for simple (non-interlocking) cycles. When a cycle is
detected, unwind to the `break_at` node and restart computation there.

### Behavior Change

```rust
// In get_idx, when cycle detected:
ProposalResult::CycleDetected => {
    let cycle = self.stack().current_cycle().unwrap();
    let break_at = cycle.minimal_idx();

    if current == break_at {
        // WE are the restart point - stay on stack and compute
        self.calculate_and_record_answer(current, idx, calculation)
    } else {
        // Unwind to break_at
        return Err(CycleUnwind {
            break_at,
            cycle_nodes: cycle.into_vec(),
        });
    }
}
```

At the `break_at` node, catch the unwind:

```rust
fn get_idx<K: Solve<Ans>>(&self, idx: Idx<K>) -> Unwindable<Arc<K::Answer>> {
    let current = CalcId(self.bindings().dupe(), K::to_anyidx(idx));

    match self.get_idx_inner(idx) {
        Ok(answer) => Ok(answer),
        Err(unwind) if unwind.break_at == current => {
            // We're the restart point - compute with placeholder
            self.set_placeholder(idx);
            Ok(self.calculate_and_record_answer(current, idx, calculation))
        }
        Err(unwind) => Err(unwind),  // Propagate further
    }
}
```

### Benefits Over Current Approach

1. **Fewer duplicate stack frames**: Currently, when a cycle is detected, computation
   continues with duplicated work. With unwinding, we cleanly unroll.

2. **Clearer control flow**: The restart point is explicit, not implicit in the
   tangled computation.

3. **Foundation for phase 3**: The unwinding mechanism is in place for SCC handling.

### Handling Interlocking Cycles

Phase 2 handles interlocking cycles the same way as the current code: each cycle is
resolved independently. True interlocking cycle handling is deferred to phase 3.

---

## Phase 3: Dynamic SCC Fixpoint

### The Interlocking Cycle Problem

Consider:

```
A → B → C → A  (cycle 1)
    ↓
    D → E → B  (cycle 2, shares B with cycle 1)
```

If thread 1 enters at A and thread 2 enters at D:
- They may discover the cycles in different orders
- B participates in both cycles
- The order of resolution can affect B's type

**This is a source of nondeterminism.**

### The Dynamic Graph Challenge

The dependency graph is not static. Consider:

```python
def f(x):
    return x.foo()  # What edges does this create?
```

- If `x: Any`, then `x.foo()` resolves via `Any.__getattr__`
- If `x: SomeClass`, then `x.foo()` pulls in `SomeClass.foo`
- The type of `x` may change during cycle iteration

**The SCC can grow as we iterate.** A new edge discovered during iteration may
connect to a node that forms a new cycle with existing nodes.

### Why Not Tarjan's Algorithm?

A natural question: why not use Tarjan's classic SCC algorithm to find all SCCs upfront
before solving?

**Tarjan's algorithm requires a static graph.** It works by doing a single depth-first
traversal, tracking discovery times and low-links. But the type checker's dependency
graph is fundamentally dynamic:

1. **Edges are discovered during computation.** We don't know `f` depends on `g` until
   we actually analyze `f`'s body and see the call `g(x)`.

2. **Edges depend on types.** Consider:
   ```python
   def f(x):
       return x.foo()  # What does f depend on?
   ```
   - If `x: Any`, this resolves via `Any.__getattr__` — no new dependencies
   - If `x: SomeClass`, this depends on `SomeClass.foo`
   - The type of `x` may only become known during cycle iteration

3. **The graph changes mid-computation.** An edge that didn't exist in iteration 1
   (because the placeholder was `Any`) may appear in iteration 2 (because the
   placeholder is now a specific class).

**Consequence:** We cannot run Tarjan's algorithm before solving because the graph
doesn't exist yet. We cannot run it after each iteration because we've already made
progress we'd need to invalidate. The only option is to discover SCCs incrementally
during computation — which is exactly what this design does.

**The algorithm here is essentially "online Tarjan":** SCCs are discovered and merged
as edges are traversed, rather than computed from a static graph. The restart-on-merge
strategy ensures we never commit partial results from an incomplete SCC.

### Relationship to Tarjan's Algorithm

While we can't use classic Tarjan directly, there are structural similarities worth
noting. These may be helpful for understanding the algorithm, though we make no strong
claims about formal equivalence.

**Tarjan's two stacks:**
1. The DFS call stack — nodes currently being explored (recursion stack)
2. The "Tarjan stack" — nodes visited but not yet assigned to an SCC

**Our two stacks:**
1. `CalcStack` — nodes currently being computed (maps to Tarjan's call stack)
2. `SccStack` — SCCs being resolved (possibly related to Tarjan's stack)

**Possible correspondence:**
- All nodes across all entries in our `SccStack` might correspond to all nodes on
  Tarjan's stack
- The difference: we group nodes into provisional SCCs immediately, whereas Tarjan
  keeps them flat and groups them only when popping
- Tarjan's lowlink propagation implicitly tracks "how far back can I reach" — our
  explicit merge operation may serve a similar purpose, compensating for not having
  complete edge information upfront

**On a static graph:**
- The final SCCs discovered should be the same
- Tarjan visits each node exactly once → O(V+E)
- Our algorithm may revisit nodes due to merge-and-restart → potentially O(V²) in
  pathological cases
- The merge overhead is the "cost" of interleaving discovery with computation

**Open question:** Is there a formal sense in which this algorithm, restricted to a
static graph, is equivalent to Tarjan? The intuition suggests they're closely related,
but this hasn't been rigorously analyzed. Understanding the precise relationship might
help with correctness arguments or optimizations.

### Proposed Algorithm

```rust
fn resolve_scc(&self, mut scc: HashSet<CalcId>) {
    let max_iterations = 3;  // Bounded iteration count

    'restart: loop {
        // Set placeholders for all nodes in current SCC
        for node in &scc {
            self.set_placeholder(node, initial_placeholder());
        }

        // Run bounded iterations
        for iteration in 0..max_iterations {
            for node in scc.iter().sorted_by_key(|n| n.minimal_idx()) {
                match self.compute_node(node) {
                    Ok(answer) => {
                        self.record_tentative_answer(node, answer);
                    }
                    Err(CycleUnwind { new_nodes, .. }) => {
                        // SCC grew! Restart from scratch.
                        scc.extend(new_nodes);
                        continue 'restart;
                    }
                }
            }

            if self.has_converged(&scc) {
                break;
            }

            // Update placeholders for next iteration
            for node in &scc {
                let current = self.get_tentative_answer(node);
                self.set_placeholder(node, current);
            }
        }

        // Commit final answers
        for node in &scc {
            self.commit_answer(node);
        }

        // Emit warning for large SCCs
        if scc.len() > LARGE_SCC_THRESHOLD {
            let anchor = scc.iter().min_by_key(|n| n.idx()).unwrap();
            self.emit_warning(anchor, format!(
                "Large strongly-connected component detected ({} bindings). \
                 Consider adding type annotations to break the cycle.",
                scc.len()
            ));
        }

        break;
    }
}
```

### Key Design Decisions

1. **Bounded iterations (2-3)**: Pragmatic limit. Current code does ~1 iteration and
   handles Instagram. 2-3 iterations should capture most convergence without being
   expensive.

2. **Restart on SCC growth**: When a new cycle is discovered that connects to the
   current SCC, wipe all tentative answers and restart. This ensures determinism:
   same code → same final SCC → same result.

3. **Deterministic processing order**: Nodes are processed in order of their minimal
   `AnyIdx`. This ensures the same computation order regardless of entry point.

4. **Minimal idx as anchor**: The minimal `AnyIdx` in the SCC serves as:
   - The cycle break point
   - The iteration starting point
   - The location for SCC warnings

### Complexity Analysis

**Worst case:** O(N²) where N is the final SCC size

- At most N restarts (each adds ≥1 node to SCC)
- Each restart processes up to N nodes
- Each node takes O(1) iterations (bounded by max_iterations)

**Why this is acceptable:**

1. SCCs are typically small (most bindings aren't in cycles)
2. The quadratic cost is isolated to the SCC
3. Practical structure (few long cycles vs. many tiny interconnected ones) means
   restarts are rare

**Cannot bound restart count**: Attempting to limit restarts would create
nondeterminism, because which cycles are discovered first depends on entry order.
Determinism requires accepting the worst-case bound.

### Safety Valve: Hard Limits

For pathologically large SCCs, we may want a hard limit as a safety valve to prevent
timeouts. However, implementing any limit deterministically is tricky:

- **Node count limit:** If we stop when SCC size exceeds N, the set of nodes included
  depends on discovery order (entry point). Different entry points might hit the limit
  with different node sets.

- **Restart count limit:** Even more dependent on discovery order.

- **The fundamental issue:** Any limit that fires *before* the full SCC is discovered
  will depend on how much of the SCC was found, which depends on entry order.

**Options:**
1. Accept some nondeterminism in the rare pathological case (emit a warning)
2. Find a deterministic fallback (e.g., sort discovered nodes, keep first N)
3. No limit, accept O(N²) worst case

**Recommendation:** Leave details open for further consideration. This may be
acceptable to defer until we have telemetry on actual SCC sizes in practice.

---

## Detailed Algorithm

This section provides a concrete description of the SCC-based fixpoint algorithm,
building from simple cycles to the full interlocking case.

### Core Data Structures

```rust
/// State for a single SCC being resolved.
struct SccState {
    /// All nodes in this SCC.
    nodes: HashSet<CalcId>,

    /// The minimal idx, used as the anchor for deterministic ordering.
    anchor: CalcId,

    /// Answers from iteration N-1 (read from during iteration N).
    prior_answers: HashMap<CalcId, Arc<dyn Any>>,

    /// Answers being computed in iteration N.
    current_answers: HashMap<CalcId, Arc<dyn Any>>,

    /// Errors from iteration N (discarded if we iterate again).
    current_errors: HashMap<CalcId, Vec<Error>>,

    /// Per-node computation state for current iteration.
    node_state: HashMap<CalcId, NodeState>,

    /// Current iteration number.
    iteration: u8,
}

enum NodeState {
    /// Ready to compute (reset at start of each iteration).
    Fresh,
    /// Currently on the Rust call stack.
    InProgress,
    /// Finished, answer in current_answers.
    Done,
}

/// The stack of SCCs being resolved.
struct SccStack {
    stack: Vec<SccState>,
}
```

### Simple Cycle Resolution

For a simple cycle `A → B → C → A` with no nested cycles:

**Step 1: Detection and Unwinding**

```
get_idx(A) starts
  → compute(A) needs B
    → get_idx(B) starts
      → compute(B) needs C
        → get_idx(C) starts
          → compute(C) needs A
            → get_idx(A): Calculation says CycleDetected!
            ← return Err(CycleUnwind { break_at: A, cycle: [A,B,C] })
          ← propagate Err
        ← propagate Err
      ← propagate Err
    ← propagate Err
  ← catch Err at A (A is the break_at)
```

**Step 2: SCC Creation**

At the catch point (the `get_idx(A)` that started the cycle):

```rust
// Create fresh SCC state
let scc = SccState {
    nodes: {A, B, C},
    anchor: A,  // minimal idx
    prior_answers: HashMap::new(),  // Empty for first iteration
    current_answers: HashMap::new(),
    current_errors: HashMap::new(),
    node_state: {A: Fresh, B: Fresh, C: Fresh},
    iteration: 0,
};
scc_stack.push(scc);
```

**Step 3: Fixpoint Iteration**

```rust
fn run_fixpoint(&mut self, scc: &mut SccState) {
    loop {
        // Reset all nodes to Fresh
        for node in &scc.nodes {
            scc.node_state.insert(node, NodeState::Fresh);
        }
        scc.current_answers.clear();
        scc.current_errors.clear();

        // Compute all nodes starting from anchor
        self.compute_node(scc.anchor);

        // Check exit condition
        scc.iteration += 1;
        if scc.iteration >= MAX_ITERATIONS || self.has_converged(scc) {
            break;
        }

        // Prepare for next iteration: current becomes prior
        std::mem::swap(&mut scc.prior_answers, &mut scc.current_answers);
    }

    // Commit final answers
    for (node, answer) in &scc.current_answers {
        global_storage.commit(node, answer);
    }
    for (node, errors) in &scc.current_errors {
        global_errors.extend(errors);
    }
}
```

**Step 4: Lookup During Computation**

When computing a node needs to look up another node in the same SCC:

```rust
fn lookup_in_scc(&mut self, scc: &mut SccState, node: CalcId) -> Answer {
    match scc.node_state.get(&node) {
        Some(NodeState::Done) => {
            // Already computed this iteration
            scc.current_answers.get(&node).clone()
        }
        Some(NodeState::InProgress) => {
            // Recursion within current iteration!
            // Fall back to prior iteration or placeholder
            if let Some(answer) = scc.prior_answers.get(&node) {
                answer.clone()
            } else {
                // First iteration, no prior - use placeholder
                create_placeholder()
            }
        }
        Some(NodeState::Fresh) | None => {
            // Need to compute it now
            scc.node_state.insert(node, NodeState::InProgress);

            let (answer, errors) = compute_binding(node);

            scc.current_answers.insert(node, answer.clone());
            scc.current_errors.insert(node, errors);
            scc.node_state.insert(node, NodeState::Done);

            answer
        }
    }
}
```

### Nested Independent SCCs

When computing an SCC, we may branch off and discover another independent SCC:

```
Resolving SCC1 = {A, B, C}
  → Computing A needs X (X not in SCC1)
    → Computing X needs Y
      → Y → Z → Y detected!  New cycle!
      → Push SCC2 = {Y, Z}
      → Resolve SCC2 with its own fixpoint
      → Pop SCC2 (answers now in global)
    → X completes (answer to global)
  → A continues with X's answer
→ SCC1 completes
```

This works naturally because:
- Each SCC has its own completely independent state
- When we detect a new cycle not connected to any existing SCC, we push a new SCC
- The nested SCC is fully resolved before returning to the outer one
- Lookup cascade: check topmost SCC first, then outer SCCs, then global

```rust
fn lookup(&self, node: CalcId) -> LookupResult {
    // Check SCC stack from top to bottom
    for scc in self.scc_stack.iter().rev() {
        if scc.contains(&node) {
            return self.lookup_in_scc(scc, node);
        }
    }
    // Not in any active SCC
    if let Some(answer) = global_storage.get(&node) {
        return LookupResult::Found(answer);
    }
    LookupResult::NeedToCompute
}
```

### Interlocking SCCs (The Hard Case)

The challenging case: we discover a cycle that connects back to an existing SCC.

**Scenario:**

```
SCC stack: [SCC1={A,B,C}]

Resolving SCC1, computing A:
  → A needs X (X not in SCC1, not in any SCC)
    → X needs D
      → D → E → D detected!
      → Push SCC2 = {D, E}

SCC stack: [SCC1={A,B,C}, SCC2={D,E}]

      → Resolving SCC2, computing D:
        → D needs Y (Y not in any SCC)
          → Y needs Z
            → Z needs A
              → get_idx(A): Calculation says InProgress!
              → Check SCC stack: A is in SCC1!
              → CONNECTION TO EXISTING SCC DETECTED!
```

**Detection:**

```rust
fn get_idx(&self, idx: Idx<K>) -> Unwindable<Arc<K::Answer>> {
    let node = CalcId::from(idx);
    let calculation = self.get_calculation(idx);

    match calculation.propose_calculation() {
        ProposalResult::Calculated(v) => Ok(v),

        ProposalResult::Calculatable => {
            // Not in progress anywhere, compute normally
            self.compute_node(node)
        }

        ProposalResult::CycleDetected => {
            // In progress somewhere! Find where.
            for (depth, scc) in self.scc_stack.iter().enumerate().rev() {
                if scc.contains(&node) {
                    // Found in an existing SCC - need to merge!
                    return Err(CycleUnwind::ConnectionToExistingScc {
                        target_scc_depth: depth,
                        node,
                    });
                }
            }
            // Not in any SCC - this is a new cycle
            Err(CycleUnwind::NewCycle {
                break_at: self.call_stack.minimal_idx(),
                cycle_nodes: self.call_stack.current_cycle(),
            })
        }
    }
}
```

**Merge Operation:**

When we detect a connection to an existing SCC, we must merge everything:

```rust
fn handle_connection_to_existing_scc(
    &mut self,
    target_scc_depth: usize,
    connecting_node: CalcId,
) -> CycleUnwind {
    // Collect ALL nodes that form the merged SCC
    let mut all_nodes = HashSet::new();

    // 1. Pop all SCCs from target_scc_depth to top of stack
    while self.scc_stack.len() > target_scc_depth {
        let scc = self.scc_stack.pop().unwrap();
        all_nodes.extend(scc.nodes);
    }

    // 2. Pop the target SCC itself
    let target_scc = self.scc_stack.pop().unwrap();
    all_nodes.extend(target_scc.nodes);

    // 3. Add all "free-floating" nodes from the Rust call stack
    //    (nodes that were being computed but weren't in any SCC)
    for node in self.call_stack.nodes_since(target_scc.anchor) {
        all_nodes.insert(node);
    }

    // 4. Create fresh SCC with completely clean state
    let merged_scc = SccState {
        nodes: all_nodes,
        anchor: all_nodes.iter().min().unwrap().clone(),
        prior_answers: HashMap::new(),     // FRESH - no prior answers
        current_answers: HashMap::new(),
        current_errors: HashMap::new(),
        node_state: all_nodes.iter().map(|n| (n, NodeState::Fresh)).collect(),
        iteration: 0,
    };

    // 5. Push the new mega-SCC
    self.scc_stack.push(merged_scc);

    // 6. Return unwind instruction
    CycleUnwind::Restart {
        break_at: merged_scc.anchor,
    }
}
```

**After the merge:**

```
SCC stack (before): [SCC1={A,B,C}, SCC2={D,E}]
Free-floating on call stack: {X, Y, Z}

SCC stack (after): [MergedSCC={A,B,C,D,E,X,Y,Z}]

Rust stack unwinds to get_idx(A)  (A is the minimal idx)

Start completely fresh fixpoint for the merged SCC
```

**Why completely fresh state is required:**

- Any answers computed for D, E were based on not knowing about the connection to A
- Any answers computed for A, B, C were based on not knowing about D, E, X, Y, Z
- Keeping partial answers would make results entry-point dependent
- Fresh state → deterministic: same final SCC always computes the same way

### Call Stack Tracking

To support the merge operation, we need to track which nodes are on the Rust call
stack but not yet in any SCC:

```rust
struct CallStack {
    /// Nodes currently being computed, in call order.
    /// Includes both SCC nodes and free-floating nodes.
    frames: Vec<CalcId>,
}

impl CallStack {
    fn push(&mut self, node: CalcId) {
        self.frames.push(node);
    }

    fn pop(&mut self) {
        self.frames.pop();
    }

    /// Get all nodes from the given anchor to the top of the stack.
    fn nodes_since(&self, anchor: &CalcId) -> Vec<CalcId> {
        let start = self.frames.iter().position(|n| n == anchor).unwrap();
        self.frames[start..].to_vec()
    }

    /// Get the minimal idx among nodes forming the current cycle.
    fn current_cycle_minimal(&self, cycle_node: &CalcId) -> CalcId {
        let start = self.frames.iter().position(|n| n == cycle_node).unwrap();
        self.frames[start..].iter().min().unwrap().clone()
    }
}
```

### Summary: The Complete Flow

```
1. get_idx(node) is called

2. Check Calculation state:
   - Calculated → return cached answer
   - Calculatable → proceed to compute
   - CycleDetected → cycle handling (step 3)

3. Cycle handling:
   a. Scan SCC stack to find if node is in an existing SCC
   b. If in existing SCC → MERGE (step 4)
   c. If not in any SCC → NEW CYCLE (step 5)

4. Merge (connection to existing SCC):
   a. Pop all SCCs from target depth to top
   b. Collect nodes from popped SCCs + call stack
   c. Create fresh merged SCC
   d. Unwind to minimal idx
   e. Start fresh fixpoint

5. New cycle (no connection to existing):
   a. Create new SCC with cycle nodes
   b. Push to SCC stack
   c. Run fixpoint to completion
   d. Pop SCC, commit answers to global

6. Fixpoint iteration:
   a. Reset all nodes to Fresh
   b. Compute starting from anchor
   c. On recursion within iteration: use prior answer or placeholder
   d. After iteration: check convergence or max iterations
   e. If done: commit answers and errors
   f. If not done: swap current→prior, iterate again
```

---

## Worked Example: Interlocking Cycles

This example traces the algorithm through an interlocking cycle scenario.

### Setup

Consider the following Python code with two interlocking cycles:

```python
# Cycle 1: f → g → h → f
def f(x):
    return g(x) + 1

def g(x):
    return h(x)

def h(x):
    return f(x) + k(x)  # k introduces connection to cycle 2

# Cycle 2: k → m → k (also connects back to h)
def k(x):
    return m(x)

def m(x):
    return k(x) + h(x)  # h is in cycle 1!
```

**Dependencies:**
- f → g → h → f (cycle 1)
- h → k → m → k (cycle 2)
- m → h (connects cycle 2 back to cycle 1)

**True SCC:** {f, g, h, k, m}

### Trace: Entry at f

**Step 1: Initial computation**

```
get_idx(f)                          Call stack: [f]
  f needs g → get_idx(g)            Call stack: [f, g]
    g needs h → get_idx(h)          Call stack: [f, g, h]
      h needs f → get_idx(f)        Call stack: [f, g, h]
        Calculation(f) = Calculating[thread1]
        Same thread → CycleDetected!
```

**Step 2: Cycle detection**

```
Cycle detected: [f, g, h]
Minimal idx: f (assuming f < g < h < k < m)
Return Err(CycleUnwind { break_at: f, cycle: [f, g, h] })

Stack unwinds:
  h: propagate Err
  g: propagate Err
  f: catch! (f == break_at)
```

**Step 3: Create SCC and start fixpoint**

```
SCC stack: [SCC1 = {f, g, h}]

SCC1 state:
  nodes: {f, g, h}
  anchor: f
  prior_answers: {}          (empty, first iteration)
  current_answers: {}
  node_state: {f: Fresh, g: Fresh, h: Fresh}
  iteration: 0
```

**Step 4: Fixpoint iteration 1**

```
Start computing from anchor (f):

compute(f):
  node_state[f] = InProgress
  f needs g → lookup_in_scc(g)
    node_state[g] = Fresh → compute it

    compute(g):
      node_state[g] = InProgress
      g needs h → lookup_in_scc(h)
        node_state[h] = Fresh → compute it

        compute(h):
          node_state[h] = InProgress
          h needs f → lookup_in_scc(f)
            node_state[f] = InProgress!
            prior_answers[f] = None (first iteration)
            → Return placeholder (e.g., Any)

          h also needs k → get_idx(k)  [k NOT in SCC1]
            Calculation(k) = NotCalculated
            → Compute k normally

            compute(k):
              k needs m → get_idx(m)
                Calculation(m) = NotCalculated
                → Compute m normally

                compute(m):
                  m needs k → get_idx(k)
                    Calculation(k) = Calculating[thread1]
                    Same thread → CycleDetected!
                    Check SCC stack: k not in SCC1
                    → New cycle! [k, m]
```

**Step 5: Nested SCC for [k, m]**

```
Push SCC2 = {k, m}
SCC stack: [SCC1 = {f, g, h}, SCC2 = {k, m}]

Start fixpoint for SCC2...

compute(k):  (anchor of SCC2)
  k needs m → lookup_in_scc(m)
    compute(m):
      m needs k → lookup_in_scc(k)
        node_state[k] = InProgress
        → Return placeholder

      m also needs h → get_idx(h)
        Calculation(h) = Calculating[thread1]
        Same thread → CycleDetected!

        Check SCC stack:
          - Is h in SCC2? No
          - Is h in SCC1? YES!

        → CONNECTION TO EXISTING SCC DETECTED!
```

**Step 6: Merge SCCs**

```
Connection from SCC2 to SCC1 detected.

Merge operation:
  1. Pop SCC2: collect {k, m}
  2. Pop SCC1: collect {f, g, h}
  3. Free-floating nodes on call stack: none (all were in SCCs)
  4. Merged SCC = {f, g, h, k, m}
  5. New anchor = f (minimal idx)

SCC stack: [MergedSCC = {f, g, h, k, m}]

Return Err(CycleUnwind::Restart { break_at: f })
Stack unwinds all the way back to get_idx(f)
```

**Step 7: Restart with merged SCC**

```
MergedSCC state (fresh start):
  nodes: {f, g, h, k, m}
  anchor: f
  prior_answers: {}          (completely fresh!)
  current_answers: {}
  node_state: {f: Fresh, g: Fresh, h: Fresh, k: Fresh, m: Fresh}
  iteration: 0
```

**Step 8: Fixpoint iteration 1 (with full SCC)**

```
compute(f):
  f needs g → lookup_in_scc(g) → compute(g)
    g needs h → lookup_in_scc(h) → compute(h)
      h needs f → lookup_in_scc(f)
        f is InProgress, prior_answers[f] = None
        → Return placeholder

      h needs k → lookup_in_scc(k) → compute(k)
        k needs m → lookup_in_scc(m) → compute(m)
          m needs k → lookup_in_scc(k)
            k is InProgress, prior_answers[k] = None
            → Return placeholder

          m needs h → lookup_in_scc(h)
            h is Done (already computed this iteration)
            → Return current_answers[h]

          m completes with some type T_m1
          current_answers[m] = T_m1
          node_state[m] = Done

        k completes with T_k1
        current_answers[k] = T_k1
        node_state[k] = Done

      h completes with T_h1
      current_answers[h] = T_h1
      node_state[h] = Done

    g completes with T_g1
    current_answers[g] = T_g1
    node_state[g] = Done

  f completes with T_f1
  current_answers[f] = T_f1
  node_state[f] = Done

Iteration 1 complete.
All nodes computed: {f: T_f1, g: T_g1, h: T_h1, k: T_k1, m: T_m1}
```

**Step 9: Check convergence, prepare iteration 2**

```
prior_answers was empty, so no convergence possible on iteration 1.

Swap: prior_answers = current_answers
      current_answers = {}

Reset: node_state = {f: Fresh, g: Fresh, h: Fresh, k: Fresh, m: Fresh}
iteration = 1
```

**Step 10: Fixpoint iteration 2**

```
compute(f):
  Same traversal, but now:
  - h needs f → prior_answers[f] = T_f1 (not placeholder!)
  - m needs k → prior_answers[k] = T_k1 (not placeholder!)

  All nodes compute with iteration 1's types.
  Results: {f: T_f2, g: T_g2, h: T_h2, k: T_k2, m: T_m2}
```

**Step 11: Check convergence**

```
Compare current_answers with prior_answers:
  f: T_f2 == T_f1?  (if yes, f converged)
  g: T_g2 == T_g1?
  h: T_h2 == T_h1?
  k: T_k2 == T_k1?
  m: T_m2 == T_m1?

If all equal: SCC converged, proceed to commit.
If any differ and iteration < MAX_ITERATIONS: continue iterating.
If any differ and iteration >= MAX_ITERATIONS: emit errors for non-converged.
```

**Step 12: Commit**

```
For each node in SCC (sorted by idx):
  Check if another thread already committed (anchor first)
  If not: commit answer and errors to global storage

Pop MergedSCC from stack.
SCC stack: []

Return T_f2 from get_idx(f).
```

### Key Observations

1. **Incremental SCC discovery:** We didn't know about k and m until we were already
   resolving {f, g, h}. The merge-and-restart mechanism handled this.

2. **Fresh state after merge:** After discovering the full SCC, we started completely
   fresh. No stale answers from the partial SCC.

3. **Deterministic traversal:** The order of computation (f → g → h → k → m) is
   deterministic, regardless of which node we entered from.

4. **Placeholder usage:** In iteration 1, placeholders were used for back-edges. In
   iteration 2, actual types from iteration 1 were used.

---

## Transactional Error Collection

### Prerequisite Work

The transactional error collection (D90268296) is a prerequisite for this design:

```rust
let local_errors = self.error_collector();
let (answer, did_write) = calculation.record_value(
    K::solve(self, binding, &local_errors),
    |var, answer| self.finalize_recursive_answer(idx, var, answer, &local_errors)
);
if did_write {
    self.base_errors.extend(local_errors);
}
```

### Why It Matters

1. **Only one computation's errors are kept**: When multiple computations race (or
   when cycle resolution computes a binding multiple times), only the "winner" that
   actually writes the answer gets to emit errors.

2. **Cleaner errors after unwinding**: In phase 2+, when we unwind and restart, the
   restart computation's errors are authoritative. Errors from aborted computations
   are discarded.

3. **Fixpoint iteration errors**: In phase 3, only the final iteration's errors are
   kept. Intermediate iterations (with placeholders) produce errors that reference
   unresolved `Var`s — these are discarded.

---

## Determinism Guarantees

### Sources of Determinism

| Aspect | Mechanism |
|--------|-----------|
| Cycle break point | Minimal `AnyIdx` in cycle |
| SCC processing order | Sorted by `AnyIdx` |
| Restart on SCC growth | Same code → same final SCC |
| Error ownership | First to `record_value()` wins |
| SCC warning location | Attached to minimal `AnyIdx` |

### The Core Invariant

**Same code → same final SCC → same fixpoint → same answer**

Regardless of:
- Which thread enters the SCC first
- Which cycle is discovered first
- The order of parallel evaluation

### Entry-Point Independence (Conjecture)

**Claim:** Given a fixed iteration bound, the detected SCC is the same regardless of
which node in the true SCC we enter from.

**Reasoning:**

1. **SCC Closure Property:** Each SCC being resolved is "closed" with respect to every
   other SCC. No result from a potentially-enclosing outer SCC can influence the
   computation of an inner SCC without immediately triggering a merge. This is because:
   - If inner SCC tries to access a node that's InProgress on the outer call stack,
     we detect the connection and merge.
   - If inner SCC tries to access a node that's in an outer SCC on the stack, we
     detect the connection and merge.

2. **Deterministic Edge Traversal:** Starting from any given idx, the order of lookups
   (edge traversals) is deterministic. Even though the Rust call stack differs based
   on entry point, the edges traversed during SCC resolution are determined by the
   computation, not the call stack.

3. **Same Edges → Same Merges:** Since the same edges are traversed regardless of
   entry point, and merging is triggered by edge traversal, the same merges occur.
   Therefore, the final detected SCC is the same.

4. **Same SCC → Same Computation:** Once we have the same SCC, we process nodes in
   deterministic order (sorted by idx), use deterministic placeholders, and run
   deterministic iterations.

**Induction Sketch:**
- Base case: The initial cycle detected might differ by entry point, but the
  merge-on-InProgress mechanism pulls in all connected nodes.
- Inductive step: Given the same SCC at iteration k, the same types are computed,
  therefore the same edges appear, therefore the same merges happen, therefore
  the SCC at iteration k+1 is the same.

**⚠️ CAUTION:** This reasoning has not been formally verified. The argument relies on
the closure property holding in all cases, which should be validated through testing
with diverse cycle structures. If counterexamples are found, the algorithm may need
adjustment.

**⚠️ NUANCE: Type-Dependent Edge Exploration:** The edges discovered during computation
can depend on the type of a placeholder. Consider:

```python
def f(x):
    if isinstance(x, SomeClass):
        return x.method()  # Only explored if x narrows to SomeClass
    return x
```

If `x`'s placeholder type differs between entry points (e.g., `Any` vs. `Unknown` vs. a
specific class), the type narrowing in `isinstance` could produce different edges. This
means:

- **Different placeholders → different branch taken → different edges explored**
- If edges differ, merges could differ, and the final detected SCC could differ

This is the strongest potential threat to entry-point independence. Mitigation requires:
1. **Consistent placeholder choice**: All entry points must use the same placeholder
   type for nodes that have no prior answer. If we always use (say) `Any`, then
   isinstance checks will behave consistently.
2. **Validate empirically**: Test with code that contains isinstance checks within
   recursive cycles to verify determinism holds.

The conjecture holds **if and only if** placeholder types are chosen independently of
entry point. The algorithm achieves this by using a fixed placeholder (not derived from
any partial computation) for nodes with no prior answer.

---

## Implementation Phases

### Phase 1: Type Rewiring (Estimated: 2-3 days)

**Tasks:**
1. Define `Unwindable<T>` and `CycleUnwind` types
2. Change `get_idx` signature
3. Update all ~119 call sites with `?` propagation
4. Update `Solve::solve()` trait signature
5. Verify all tests pass (no behavioral change)

**Success criteria:**
- Code compiles
- All tests pass
- `get_idx` never returns `Err` (yet)

### Phase 2: Simple Unwinding (Estimated: 1-2 weeks)

**Tasks:**
1. Implement unwinding on cycle detection
2. Implement catch-and-restart at `break_at`
3. Add telemetry for cycle detection frequency
4. Update cycle handling tests

**Success criteria:**
- Simple cycles work correctly
- Reduced duplicate computation (verify via telemetry)
- No regressions

### Phase 3: Dynamic SCC Fixpoint (Estimated: 3-4 weeks)

**Tasks:**
1. Implement `SccState` tracking
2. Implement bounded fixpoint iteration
3. Implement restart-on-SCC-growth
4. Add convergence detection
5. Add large SCC warnings
6. Extensive testing with interlocking cycles

**Success criteria:**
- Interlocking cycles produce deterministic results
- Performance acceptable (<2x slowdown for pathological cases)
- Telemetry shows convergence typically in 1-2 iterations

---

## Open Questions

### Q1: What's the right iteration bound?

Current thinking: 2-3 iterations. This can be decided later based on empirical
validation. Could be configurable or adaptive. Not a fundamental design question.

### Q2: How to handle non-convergence?

After max iterations without convergence:
- Use the last iteration's result
- Emit a type error indicating the cycle did not stabilize
- Consider this a hint to add type annotations

The exact error message and UX can be refined later. No fundamental blockers.

### Q3: What placeholder to use for recursion?

**Short term:** Use the existing `Var`-based recursive placeholder. This maintains
compatibility with existing cycle handling during the transition.

**Long term goal:** Replace with simpler placeholders:
- `Any` for type bindings
- Appropriate "dummy" values for other binding kinds (e.g., empty metadata, default
  class fields)

**Dependency:** The simpler placeholder approach requires the SCC algorithm to be in
place first. Without proper fixpoint iteration, the existing `Var` logic is needed
to handle recursive types. Once the SCC-based iteration is working, we can simplify
the placeholder without losing type quality.

---

## Convergence Detection

There are two levels of convergence in the fixpoint algorithm:

### Per-Idx Convergence

For each binding in the SCC, compare the result from iteration N-1 with iteration N:

```rust
fn idx_has_converged(&self, scc: &SccState, node: &CalcId) -> bool {
    let prior = scc.prior_answers.get(node);
    let current = scc.current_answers.get(node);
    match (prior, current) {
        (Some(p), Some(c)) => p == c,  // or type-equality check
        (None, _) => false,             // First iteration, can't have converged
        _ => false,
    }
}
```

If a binding has **not** converged after max iterations, this is a **type error at
that binding**. The binding has a text range, so the UX is well-defined: the error
points to the specific location that didn't stabilize.

```rust
fn emit_non_convergence_errors(&self, scc: &SccState, errors: &ErrorCollector) {
    for node in &scc.nodes {
        if !self.idx_has_converged(scc, node) {
            let range = self.bindings().idx_to_key(node).range();
            errors.add(
                range,
                ErrorKind::CycleDidNotConverge,
                "Type did not stabilize in cyclic computation. \
                 Consider adding a type annotation.",
            );
        }
    }
}
```

### Whole-SCC Convergence (Early Stopping)

The SCC as a whole has converged if **every** idx in the SCC has converged:

```rust
fn scc_has_converged(&self, scc: &SccState) -> bool {
    scc.nodes.iter().all(|node| self.idx_has_converged(scc, node))
}
```

This is used for early stopping:
- If the SCC converges before max iterations, we can stop early
- If any idx hasn't converged, we keep iterating (unless at the iteration limit)

```rust
fn run_fixpoint(&mut self, scc: &mut SccState) {
    for iteration in 0..MAX_ITERATIONS {
        self.run_one_iteration(scc);

        if self.scc_has_converged(scc) {
            break;  // Early exit, all bindings stabilized
        }

        // Prepare for next iteration
        std::mem::swap(&mut scc.prior_answers, &mut scc.current_answers);
    }

    // After loop: emit errors for any bindings that didn't converge
    self.emit_non_convergence_errors(scc, &self.errors);

    // Commit final answers (even non-converged ones)
    self.commit_scc(scc);
}
```

### Note on Type Equality

The convergence check requires comparing types for equality. This could be:
- Structural equality (recursive comparison)
- Pointer equality (if types are interned)
- Hash comparison (for efficiency)

The choice affects performance but not correctness. Structural equality is simplest
to implement; optimization can come later.

---

## Simplifying Calculation

### Current State

The `Calculation` type currently tracks a recursive placeholder `R`:

```rust
enum Status<T, R> {
    NotCalculated,
    Calculating(Box<(Option<R>, SmallSet<ThreadId>)>),  // R is the placeholder
    Calculated(T),
}
```

This `R` value is shared across threads, which can cause data races that contribute
to nondeterminism.

### Future State

With SCC-based cycle handling, the recursive placeholder lives entirely in thread-local
`SccState`. The `Calculation` type can be simplified:

```rust
enum Status<T> {
    NotCalculated,
    Calculating(SmallSet<ThreadId>),  // Just thread IDs, no placeholder
    Calculated(T),
}
```

**Benefits:**
- Eliminates data races on the placeholder value
- Simplifies `Calculation` API
- Makes the separation of concerns clearer: `Calculation` tracks computation status,
  `SccState` handles cycle resolution

**Implementation timing:** The exact stage at which we can remove `R` from `Calculation`
is TBD. It may require the full SCC-based approach to be in place first. The end state
should have no `R` in `Calculating` — just a set of thread IDs.

### Thread Coordination for SCC Commits

With thread-local placeholders, different threads resolving the same SCC would construct
different placeholder values. This is acceptable as long as **exactly one thread commits
the entire SCC**.

**The safest approach:** Ensure one thread commits all bindings in an SCC atomically.
This avoids mixing Vars from different threads.

**Mechanism:** When committing an SCC, check the minimal idx first:
- If minimal idx is already `Calculated`: another thread won — abort our SCC commit
  entirely and use the other thread's results
- If minimal idx is not yet `Calculated`: we win — commit all bindings in the SCC

```rust
fn commit_scc(&mut self, scc: &SccState) -> bool {
    // Check if another thread already committed this SCC
    let anchor_calc = self.get_calculation(scc.anchor);
    if anchor_calc.get().is_some() {
        // Another thread won, discard our results
        return false;
    }

    // We're the first — commit all bindings
    for (node, answer) in &scc.current_answers {
        let (_, did_write) = self.get_calculation(node).record_value(answer.clone());
        if did_write {
            // Also commit errors for this binding
            if let Some(errors) = scc.current_errors.get(node) {
                self.base_errors.extend(errors);
            }
        }
    }
    true
}
```

This approach:
- Uses only idx-based locking (no new synchronization primitives)
- Ensures deterministic write order (sorted by idx within SCC)
- Prevents mixing Vars from different threads
- Is particularly important while Var-based placeholders remain in use

### Interaction with record_recursive

While Var-based placeholders remain in use, `record_recursive` calls need handling:

**When record_recursive occurs:** During the first pass of a fixpoint, when a binding
that created a recursive placeholder (Var) is solved for the first time. At this point,
the Var is forced (resolved).

**During SCC restart (after merge):** When we restart a fixpoint from scratch after
merging SCCs, we create new Vars. The old Vars from the aborted computation just sit
around unused — this is fine, they're never queried.

**Long-term:** Once we eliminate Var-based placeholders in favor of simpler placeholders
(Any, dummy values), `record_recursive` disappears entirely. This is one of the
simplifications enabled by the SCC-based approach.

---

## Cross-Module SCCs

### No Special Handling Needed

Cycles can span module boundaries (e.g., module A imports from module B, which imports
from module A). This is already handled by the existing architecture.

**Key insight:** `ThreadState` is per solver thread and is passed across module
boundaries. When we cross a module boundary during export resolution, we create a new
`AnswersSolver` but share the same `ThreadState`.

The following structures already work cross-module:
- `CalcStack` — tracks in-progress computations across modules
- `CycleStack` (current) — tracks cycles across modules

The proposed `SccStack` would work identically:
- Lives in `ThreadState`
- Passed across module boundaries when creating new `AnswersSolver` instances
- `CalcId` already contains `(Bindings, AnyIdx)` which uniquely identifies a binding
  including its module

**Tentative answers lookup:** When resolving within an SCC, lookups check:
1. The SCC's `prior_answers` / `current_answers` (keyed by `CalcId`, includes module)
2. Global `Calculation` storage (per-binding, per-module)

Since `CalcId` includes module identity, cross-module SCCs "just work" — no special
handling is needed beyond what already exists for the `CalcStack` and `CycleStack`.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Phase 1 takes longer than expected | Medium | Low | Mechanical, can parallelize |
| Phase 2 changes behavior unexpectedly | Medium | Medium | Extensive testing, telemetry |
| Phase 3 complexity is high | High | High | Start simple, iterate |
| Performance regression in common case | Low | High | Ensure no overhead when no cycles |
| Interlocking cycles remain nondeterministic | Medium | High | Careful design, extensive testing |

**Overall risk:** Medium-High. The design is sound but implementation is complex.

---

## Conclusion

Stack unwinding via `Unwindable<T>` enables a cleaner cycle handling architecture:

1. **Phase 1** is mechanical preparation with no behavioral change
2. **Phase 2** simplifies single-cycle handling with cleaner control flow
3. **Phase 3** enables true determinism for interlocking cycles via SCC fixpoint

The key insight is that **determinism requires the ability to restart cleanly**.
Without stack unwinding, partial computations are scattered through the call stack
and contaminate the final answer.

The transactional error collection (already implemented) is a prerequisite that
ensures only authoritative errors are kept.

**Next step:** Implement Phase 1 type rewiring.
