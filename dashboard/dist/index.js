/**
 * Muster — dashboard tab (web fallback for the Desktop pane).
 *
 * Plain IIFE using the dashboard plugin SDK globals; no build step.
 * Reads /api/plugins/muster/board every 3 seconds and renders the fleet
 * table. Actions (cancel/steer) go through the same backend routes.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks || { useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback };
  const cn = (SDK.utils && SDK.utils.cn) || function () { return Array.prototype.slice.call(arguments).filter(Boolean).join(" "); };

  const STATE_STYLE = {
    RUNNING: ["bg-emerald-400", "text-emerald-400", "border-emerald-500/30", "working"],
    STARTING: ["bg-sky-400", "text-sky-400", "border-sky-500/30", "starting"],
    PENDING: ["bg-sky-400/70", "text-sky-400/90", "border-sky-500/20", "queued"],
    STALLED: ["bg-amber-400", "text-amber-400", "border-amber-500/40", "stuck"],
    SPINNING: ["bg-red-400", "text-red-400", "border-red-500/40", "failing on repeat"],
    LOST: ["bg-zinc-400", "text-zinc-400", "border-zinc-500/30", "gone"],
    SUCCEEDED: ["bg-emerald-400/60", "text-emerald-400/80", "border-emerald-500/20", "done"],
    FAILED: ["bg-red-400/70", "text-red-400/90", "border-red-500/25", "failed"],
    INTERRUPTED: ["bg-orange-400/70", "text-orange-400/90", "border-orange-500/25", "cut short"],
    CANCEL_REQUESTED: ["bg-orange-400", "text-orange-400", "border-orange-500/40", "stopping…"],
    CANCELLED: ["bg-zinc-400/70", "text-zinc-400/90", "border-zinc-500/25", "stopped"],
  };

  function stateStyle(state) {
    const s = STATE_STYLE[state] || ["bg-zinc-500", "text-zinc-400", "border-zinc-500/20", state || "?"];
    return { dot: s[0], text: s[1], ring: s[2], label: s[3] };
  }

  const LIVE = new Set(["PENDING", "STARTING", "RUNNING", "STALLED", "SPINNING", "CANCEL_REQUESTED"]);

  function ageOf(seconds) {
    if (seconds == null) return "–";
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60) + "s";
    return Math.floor(s / 3600) + "h" + Math.floor((s % 3600) / 60) + "m";
  }

  function fmtTokens(n) {
    if (!n) return "0";
    if (n < 1000) return String(n);
    if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + "k";
    return (n / 1000000).toFixed(1) + "M";
  }

  function StateBadge(props) {
    const s = stateStyle(props.state);
    return React.createElement(
      "span",
      { className: cn("inline-flex items-center gap-1 rounded border px-1 py-[1px] text-[0.625rem] font-medium", s.ring, s.text) },
      React.createElement("span", { className: cn("inline-block h-1.5 w-1.5 rounded-full", s.dot) }),
      s.label
    );
  }

  function Row(props) {
    const f = props.flight;
    const [msg, setMsg] = useState(null);
    const [steerText, setSteerText] = useState("");
    const [steering, setSteering] = useState(false);
    const s = stateStyle(f.state);
    const canAct = props.serviceAvailable && f.has_handle && LIVE.has(f.state);

    const act = useCallback(function (verb, extra) {
      SDK.fetchJSON("/api/plugins/muster/action/" + verb, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ flight_id: f.short_id, text: extra || "", reason: extra || "muster dashboard" }),
      }).then(function (res) { setMsg(res && res.message); props.refresh(); })
        .catch(function (e) { setMsg("failed: " + e); });
    }, [f.short_id, props.refresh]);

    return React.createElement("div", { className: cn("rounded-md border px-2.5 py-2 flex flex-col gap-1.5", s.ring) },
      React.createElement("div", { className: "flex items-center gap-2" },
        React.createElement(StateBadge, { state: f.state }),
        React.createElement("span", { className: "text-[0.6875rem] text-muted-foreground font-mono" }, f.short_id),
        React.createElement("span", { className: "text-[0.6875rem] text-muted-foreground" }, f.role || ""),
        React.createElement("span", { className: "flex-1" }),
        React.createElement("span", { className: "text-[0.6875rem] text-muted-foreground" }, ageOf(f.last_event_age) + " quiet")
      ),
      props.showGoals && f.goal
        ? React.createElement("div", { className: "text-xs leading-snug break-words" }, f.goal)
        : null,
      React.createElement("div", { className: "flex items-center gap-2 text-[0.625rem] text-muted-foreground" },
        React.createElement("span", null, (f.tool_calls || 0) + " tools"),
        React.createElement("span", null, (f.api_calls || 0) + " api"),
        React.createElement("span", null, fmtTokens(f.prompt_tokens) + "→" + fmtTokens(f.completion_tokens) + " tok"),
        (f.requeue_count || 0) >= 2 ? React.createElement("span", { className: "text-red-400 font-medium" }, "failed " + f.requeue_count + "×") : null,
        f.tool_in_flight ? React.createElement("span", { className: "text-sky-400" }, "in " + f.tool_in_flight) : null,
        f.source && f.source !== "muster" ? React.createElement("span", { className: "ml-auto" }, f.source === "observed" ? "hermes-spawned · read-only" : f.source) : null
      ),
      f.state === "LOST"
        ? React.createElement("div", { className: "text-[0.625rem] text-zinc-400" }, "hermes crashed before this finished — found at restart")
        : null,
      canAct
        ? React.createElement("div", { className: "flex items-center gap-1.5" },
            React.createElement("button", { className: "rounded border border-red-500/40 px-2 py-0.5 text-[0.6875rem] text-red-400 hover:bg-red-500/10", onClick: function () { act("cancel", ""); } }, "Stop"),
            React.createElement("button", { className: "rounded border border-sky-500/40 px-2 py-0.5 text-[0.6875rem] text-sky-400 hover:bg-sky-500/10", onClick: function () { setSteering(function (v) { return !v; }); } }, steering ? "Close" : "Message"),
            steering ? React.createElement(React.Fragment, null,
              React.createElement("input", { value: steerText, onChange: function (e) { setSteerText(e.target.value); }, placeholder: "send it a message…", className: "flex-1 rounded border bg-transparent px-2 py-1 text-xs" }),
              React.createElement("button", { className: "rounded border border-sky-500/40 px-2 py-0.5 text-[0.6875rem] text-sky-400", onClick: function () { if (steerText.trim()) { act("steer", steerText.trim()); setSteerText(""); setSteering(false); } } }, "Send")
            ) : null
          )
        : null,
      !canAct && f.has_handle === false && LIVE.has(f.state)
        ? React.createElement("div", { className: "text-[0.625rem] text-muted-foreground" }, "started by hermes itself — stop it from the chat that started it")
        : null,
      msg ? React.createElement("div", { className: "text-[0.625rem] text-muted-foreground" }, msg) : null
    );
  }

  function MusterPage() {
    const [board, setBoard] = useState(null);
    const [err, setErr] = useState(null);

    const refresh = useCallback(function () {
      SDK.fetchJSON("/api/plugins/muster/board")
        .then(function (d) { setBoard(d); setErr(null); })
        .catch(function (e) { setErr(String(e)); });
    }, []);

    useEffect(function () {
      refresh();
      const t = setInterval(refresh, 3000);
      return function () { clearInterval(t); };
    }, [refresh]);

    const counts = (board && board.chip) || {};
    const flights = (board && board.flights) || [];
    const showGoals = !board || board.show_goals !== false;
    const serviceAvailable = !board || board.service_available !== false;
    const active = flights.filter(function (f) { return LIVE.has(f.state) || f.state === "LOST"; });
    const recent = flights.filter(function (f) { return !LIVE.has(f.state) && f.state !== "LOST"; }).slice(0, 16);
    const alarm = (counts.stall || 0) + (counts.spin || 0) + (counts.lost || 0);

    return React.createElement("div", { className: "flex flex-col gap-4 p-4 max-w-3xl mx-auto" },
      React.createElement("div", { className: "flex items-center gap-3" },
        React.createElement("h1", { className: "text-lg font-semibold" }, "Muster"),
        React.createElement("span", { className: "text-xs text-muted-foreground" }, "air-traffic control for delegated subagent fleets"),
        React.createElement("span", { className: "flex-1" }),
        alarm > 0 ? React.createElement("span", { className: "rounded bg-amber-500/15 px-2 py-0.5 text-xs text-amber-400" }, "needs attention") : null
      ),
      React.createElement("div", { className: "flex gap-4 text-xs text-muted-foreground" },
        React.createElement("span", null, "working " + (counts.run || 0)),
        React.createElement("span", null, "stuck " + (counts.stall || 0)),
        React.createElement("span", null, "failing " + (counts.spin || 0)),
        React.createElement("span", null, "gone " + (counts.lost || 0)),
        React.createElement("span", null, "requeues 1h " + (board ? board.requeues_1h : 0))
      ),
      err ? React.createElement("div", { className: "rounded border border-red-500/30 px-2 py-1.5 text-xs text-red-400" }, "backend unavailable: " + err) : null,
      !err && flights.length === 0
        ? React.createElement("div", { className: "text-sm text-muted-foreground" }, "fleet idle — no delegated children recorded yet. muster_fanout launches supervised ones; delegate_task children appear automatically.")
        : null,
      active.map(function (f) { return React.createElement(Row, { key: f.id, flight: f, serviceAvailable: serviceAvailable, showGoals: showGoals, refresh: refresh }); }),
      recent.length ? React.createElement("div", { className: "mt-2 text-[0.625rem] uppercase tracking-wide text-muted-foreground" }, "recent") : null,
      recent.map(function (f) { return React.createElement(Row, { key: f.id, flight: f, serviceAvailable: serviceAvailable, showGoals: showGoals, refresh: refresh }); })
    );
  }

  window.__HERMES_PLUGINS__.register("muster", MusterPage);
})();
