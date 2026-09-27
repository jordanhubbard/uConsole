# Cool demo material: live uConsole schematic

Status: proposed demo/backlog, not implemented and not a gate for the current
help/diagnostics release. Requested 2026-09-27.

## Experience

A zoomable, pannable Tk Canvas depicts the uConsole's components and buses.
Clicking a component opens the corresponding implementation in the Host source
editor, at the relevant symbol. While an owned emulator runs, the diagram shows
observed state and activity: keyboard events, USB attachment, power changes,
display updates, storage traffic and modem/audio events where instrumented.

Start with a readable system diagram; allow drill-down into schematic sheets
and component/net detail. Use the repository's CM4 adapter, mainboard, keyboard
and 4G schematics as the hardware references, with explicit sheet/revision
provenance. Do not substitute a decorative board drawing for that mapping.

## Implementation outline

- Canvas vector items, tagged by stable component/net IDs, provide hit testing,
  selection, tooltips and highlights without requiring a web renderer. Provide
  keyboard navigation, a searchable component list and a text inspector too.
- A declarative component map binds hardware reference designators and buses to
  source paths/symbols, emulator device IDs, fidelity labels and telemetry keys.
  It supports one-to-many mappings: real hardware, its QEMU surrogate and the
  guest driver are not necessarily the same implementation.
- Source navigation must preserve unsaved edits. Inspect installed sources
  read-only or copy them into a user-owned project before editing; never make
  package resources writable just to support a demo. Resolve symbols against
  the actual source revision rather than relying only on brittle line numbers.
- Reuse the existing owned runtime/controller for read-only observations. Power
  already has QOM-backed sampling; audio has owned-device attachment readback;
  the modem has runtime query/inspection. These establish state, not bus traffic.
- Add bounded QEMU trace/counter instrumentation for actual transaction activity
  where needed. Coalesce samples off the UI thread and apply updates on Tk's
  thread. Cap update rates, queue sizes and history so a busy guest cannot freeze
  the editor or delay its control jobs.
- Selecting a component is navigation, never an implicit power change, device
  write or physical-target action. Optional actions call the existing guarded
  controls with their normal permissions and confirmations.

## Honest runtime presentation

Distinguish **present**, **configured**, **recent observed activity**, **fault**,
**stale/disconnected**, and **not instrumented**. Show timestamps, sample source,
and dropped-event counts. Use labels/shapes as well as color. A running VM does
not imply that every connected device is active. Missing telemetry is not idle.
Reset highlights on stop/restart and bind observations to the owned VM identity.

Brief pulses may visualize observed events; their visual duration is for human
readability, not hardware timing. Mark host surrogates prominently. This is a
live, code-linked functional view—not SPICE, electrical simulation, or evidence
of native DSI/GPU, signal-integrity or real-device fidelity.

## Demo milestones

1. **Clickable map:** CM4, PMIC/battery, keyboard/trackball, display, storage,
   audio and modem, with verified source links, pan/zoom and accessible selection.
2. **Live state:** owned-runtime status and existing power/USB/modem readbacks;
   explicit unknown/stale states when a device lacks telemetry or disconnects.
3. **Activity and replay:** instrument selected bus/device events, then record and
   replay a deterministic walkthrough: press a key, follow the observed event
   path, jump to its code, change a power state, and inspect the resulting guest
   behavior. Label replay distinctly from live observations.

Acceptance includes source-link tests across packaged/check-out installations,
unsaved-edit protection, visible unknown states, stop/restart ownership tests,
synthetic high-rate telemetry stress, responsive editing during updates, and a
recorded demo whose highlights can be reconciled with retained trace evidence.
