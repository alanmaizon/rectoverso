# Day 1 Research: Scaling Managed Agents

Harnesses encode assumptions that go stale as models improve. **Managed Agents**—Anthropic’s hosted service for long-horizon agent work—is built around interfaces that stay stable as harnesses evolve.

Get started with **Claude Managed Agents** in the official docs.

A recurring topic on the Engineering Blog is how to build effective agents and design harnesses for long-running work. A common pattern is that harnesses encode assumptions about what Claude *can’t* do on its own. Those assumptions need to be revisited often, because they can become outdated as models improve.

For example, in prior work Anthropic found that **Claude Sonnet 4.5** would wrap up tasks prematurely as it sensed its context limit approaching—a behavior they referred to as **“context anxiety.”** They addressed this by adding context resets to the harness.

But when the same harness was used on **Claude Opus 4.5**, the behavior disappeared. The context resets had become unnecessary overhead.

Harnesses will continue evolving. So Anthropic built **Managed Agents**, a hosted service in the Claude Platform that runs long-horizon agents through a small set of interfaces designed to outlast any particular implementation—including the ones in use today.

---

## Designing for “Programs as Yet Unthought Of”

Building Managed Agents meant solving a classic systems problem: **how to design a system for “programs as yet unthought of.”**

Operating systems solved this by **virtualizing hardware into stable abstractions**:

- **process**
- **file**

These abstractions remained stable while hardware changed underneath them. For example:

```bash
read()
```

works whether it is reading from a 1970s disk pack or a modern SSD.

Managed Agents follow the same principle by virtualizing the core components of an agent:

* **Session** → append-only log of everything that happened
* **Harness** → loop that calls Claude and routes tool calls
* **Sandbox** → execution environment where Claude runs code and edits files

This allows implementations to be swapped independently without disturbing the rest of the system.

Anthropic is opinionated about **interfaces**, not about what runs behind them.

---

# Don’t Adopt a Pet

The first architecture placed all agent components into a single container:

* session
* harness
* sandbox

This made some things easy:

* file edits were direct syscalls
* no service boundaries were required

But it created a fragile system—a **“pet”** in the classic **pets vs cattle** analogy.

A pet is:

* named
* hand-tended
* difficult to replace

In this case, the server became the pet:

* if the container failed, the session was lost
* if the container hung, engineers had to manually recover it

---

## Debugging Became Painful

Failures all looked identical through the WebSocket stream:

* harness bug
* packet drop
* offline container

Diagnosing the issue required opening a shell in the container—but that container often held user data, so debugging was unsafe.

A second issue was that the harness assumed Claude’s working resources lived inside the same container. Customers wanting to connect Claude to their own VPC had two options:

1. peer their network with Anthropic’s
2. run the harness themselves

A local assumption had become an infrastructure limitation.

---

# Decouple the Brain from the Hands

Anthropic split the system into three independent components:

* **Brain** → Claude + harness
* **Hands** → sandboxes + tools
* **Session** → durable event log

Each became an interface with minimal assumptions.

---

## The Harness Leaves the Container

Instead of living inside the sandbox, the harness now calls tools via:

```ts
execute(name, input) -> string
```

The container becomes **cattle**, not a pet.

If a container dies:

1. harness catches tool-call error
2. Claude decides whether to retry
3. a new container is provisioned:

```ts
provision({ resources })
```

No manual recovery required.

---

## Recovering from Harness Failure

Because the **session log** lives outside the harness, the harness itself can fail safely.

A new harness can recover using:

```ts
wake(sessionId)
getSession(id)
emitEvent(id, event)
```

This allows the system to resume from the last known event.

The session log becomes the durable source of truth.

---

# The Security Boundary

In the original architecture, Claude-generated code ran in the same container as credentials.

That meant prompt injection could potentially expose:

* environment tokens
* session credentials

Even narrow-scoped tokens still rely on assumptions about what Claude *cannot* do.

The structural fix was to ensure:

> **Tokens are never reachable from the sandbox.**

Anthropic used two patterns:

### 1. Auth Bundled with Resource

For Git:

* repo token clones repo during sandbox init
* git remote is configured locally

This allows:

```bash
git push
git pull
```

without exposing the token.

---

### 2. Vault + Proxy

For custom tools:

* OAuth tokens stored in secure vault
* Claude calls MCP tool via proxy
* proxy retrieves token from vault

Claude never directly sees credentials.

The harness also remains unaware of secrets.

---

# The Session Is Not Claude’s Context Window

Long-horizon tasks often exceed Claude’s context window.

Traditional strategies include:

* **compaction**
* **memory tools**
* **context trimming**

These approaches all require irreversible choices about what to discard.

That creates risk:

> The model may later need the information that was trimmed.

---

## Session as Durable Context Object

Managed Agents instead store durable context in the **session log**, accessible via:

```ts
getEvents()
```

This lets the harness:

* fetch slices of prior events
* rewind before a moment
* reread prior actions

Fetched events can then be transformed before entering Claude’s context window.

This separation gives:

* **durable context storage** in the session
* **context engineering** in the harness

The session guarantees recoverability; the harness controls presentation.

---

# Many Brains, Many Hands

---

## Many Brains

Previously:

> one brain = one container

That meant:

* container provisioning before inference
* repo cloning before first response
* high **time-to-first-token (TTFT)**

After decoupling:

* harness starts immediately
* containers provision only when needed

This reduced latency significantly:

* **p50 TTFT ↓ ~60%**
* **p95 TTFT ↓ >90%**

Scaling many brains now means:

> starting many stateless harnesses

---

## Many Hands

Anthropic also wanted each brain to control **many execution environments**.

Instead of a single shell, each hand is a tool:

```ts
execute(name, input) -> string
```

This interface supports:

* custom tools
* MCP servers
* internal sandboxes

The harness doesn’t care whether the tool is:

* a container
* a phone
* a Pokémon emulator

And because no hand is coupled to a brain:

> brains can pass hands to one another

This creates highly composable agents.

---

# Conclusion

The challenge was to design a system for **future agent architectures**.

Managed Agents does this through stable abstractions:

* **session** for durable state
* **sandbox** for computation
* **harness** for orchestration

These interfaces make it possible to:

* swap harnesses
* replace sandboxes
* scale brains
* secure credentials
* support future architectures

This **meta-harness** design is intentionally flexible.

Anthropic expects Claude to need:

* state manipulation
* computation
* multiple brains
* multiple hands

But they avoid assumptions about:

* how many
* where they run
* how harnesses evolve

That flexibility is what allows Managed Agents to scale with model intelligence over time.

---

# Acknowledgements

Written by:

* **Lance Martin**
* **Gabe Cemaj**
* **Michael Cohen**

Thanks to:

* **Nodir Turakulov**
* **Jeremy Fox**

Special thanks to the **Agents API team** and **Jake Eaton** for their contributions.

```
