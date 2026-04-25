/* global React */
const { useMemo, useState, useEffect } = React;

/* --- Masthead -------------------------------------------------------- */
function Masthead() {
  return (
    <header className="mast">
      <div className="mast__inner">
        <div className="mast__wm">RV</div>
        <div className="mast__name">Recto Verso Productions</div>
        <div className="mast__prov">
          <span>Built with Opus 4.7</span>
          <a href="https://github.com/alanmaizon/rectoverso" target="_blank" rel="noopener"
             className="mast__gh" aria-label="Source on GitHub">
            <Icon name="github" size={16} />
          </a>
        </div>
      </div>
    </header>
  );
}

/* --- Icon ------------------------------------------------------------ */
function Icon({ name, size = 16 }) {
  const paths = {
    play: <polygon points="6 3 20 12 6 21 6 3" />,
    pause: <g><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></g>,
    download: <g><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></g>,
    external: <g><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></g>,
    expand: <polyline points="6 9 12 15 18 9" />,
    collapse: <polyline points="18 15 12 9 6 15" />,
    github: <g fill="currentColor" stroke="none"><path d="M12 .5C5.73.5.75 5.48.75 11.75c0 4.99 3.23 9.22 7.71 10.72.56.1.77-.25.77-.54v-2.1c-3.14.68-3.8-1.33-3.8-1.33-.51-1.3-1.25-1.64-1.25-1.64-1.02-.7.08-.68.08-.68 1.13.08 1.72 1.16 1.72 1.16 1 1.72 2.63 1.22 3.28.94.1-.73.39-1.23.71-1.51-2.5-.28-5.14-1.25-5.14-5.56 0-1.23.44-2.23 1.16-3.02-.12-.28-.5-1.43.11-2.98 0 0 .94-.3 3.08 1.15.89-.25 1.85-.37 2.8-.38.95 0 1.91.13 2.8.38 2.14-1.45 3.08-1.15 3.08-1.15.61 1.55.23 2.7.11 2.98.72.79 1.15 1.79 1.15 3.02 0 4.32-2.64 5.28-5.16 5.55.4.35.76 1.04.76 2.1v3.11c0 .3.2.65.78.54 4.48-1.5 7.7-5.73 7.7-10.72C23.25 5.48 18.27.5 12 .5z"/></g>,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24"
         stroke="currentColor" fill={name === "play" ? "currentColor" : "none"}
         strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      {paths[name]}
    </svg>
  );
}

/* --- Hero ------------------------------------------------------------ */
function Hero({ brief, edit, project }) {
  const mins = Math.floor(edit.total_duration_s / 60);
  const secs = (edit.total_duration_s % 60).toString().padStart(2, "0");
  return (
    <section className="hero">
      <div>
        <div className="hero__frame" role="button" aria-label="Play film">
          <img src="./media/shots/sh_005.svg" alt="Final film poster — the door opens onto a glacier" />
          <div className="hero__play"><Icon name="play" size={56} /></div>
        </div>
        <h1 className="hero__title">Magic Doors</h1>
        <p className="hero__logline">{brief.logline}</p>
      </div>
      <aside className="hero__rail">
        <dl>
          <dt>duration</dt><dd>{mins}:{secs}</dd>
          <dt>aspect</dt><dd>16:9 · 1080p</dd>
          <dt>released</dt><dd>22 April 2026</dd>
          <dt>director</dt><dd>rectoverso (pipeline)</dd>
          <dt>project</dt><dd>{project}</dd>
        </dl>
        <a href="#" className="hero__dl-download">
          <Icon name="download" /> Download FCPXML
        </a>
      </aside>
    </section>
  );
}

/* --- Brief ----------------------------------------------------------- */
function Brief({ brief }) {
  return (
    <section className="section brief" id="brief">
      <p className="section__eyebrow">§ 01 — the brief</p>
      <h2 className="section__title">The creative input.</h2>
      <div className="brief__body">
        <p>{brief.logline}</p>
        <p style={{color:"var(--fg-2)"}}>
          A short film in the register of magical realism — dreamlike but grounded.
          Golden-hour photography, shallow depth of field, handheld but calm.
          Sixty seconds; one idea; no third act twist.
        </p>
      </div>
      <div className="brief__tone">
        tone · {brief.tone.join(" · ")} &nbsp;/&nbsp; genre · {brief.genre}
      </div>
    </section>
  );
}

/* --- Script ---------------------------------------------------------- */
function Script({ shots }) {
  return (
    <section className="section" id="script">
      <p className="section__eyebrow">§ 02 — the script</p>
      <h2 className="section__title">Shots, in order.</h2>
      <ol className="script__list">
        {shots.map((s, i) => (
          <li key={s.shot_id} className="script__row">
            <span className="script__num">{String(i + 1).padStart(2, "0")}</span>
            <span className="script__scene">scene {s.scene}</span>
            <span className="script__desc">{s.description}</span>
            <span className="script__dur">{s.duration_s.toFixed(1)}s</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

/* --- Shot strip + drawer -------------------------------------------- */
function statusDotColor(status) {
  return {
    approved: "#4A6B3A",
    rejected: "#8A3A2A",
    escalated: "#9A6A24",
    created: "#6A6354",
    prompted: "#6A6354",
    routed: "#6A6354",
    rendering: "#6A6354",
    judging: "#6A6354",
  }[status] || "#6A6354";
}

function ShotStrip({ shots, activeId, onSelect }) {
  return (
    <section className="section" id="strip">
      <p className="section__eyebrow">§ 03 — the shot strip</p>
      <h2 className="section__title">Evidence.</h2>
      <p style={{maxWidth:"54ch", color:"var(--fg-2)", marginBottom:"var(--sp-5)"}}>
        Click a frame to expand the prompt the Prompt Smith authored, the provider the Router chose,
        every attempt, judge notes, and cost. Colour lives here, inside the frames.
      </p>
      <div className="strip" role="listbox">
        {shots.map(s => (
          <button
            key={s.shot_id}
            role="option"
            aria-selected={s.shot_id === activeId}
            className={`strip__card${s.shot_id === activeId ? " strip__card--active" : ""}`}
            onClick={() => onSelect(s.shot_id)}>
            <div className="strip__thumb">
              <img src={`./media/shots/${s.shot_id}.svg`} alt="" />
            </div>
            <div className="strip__meta">
              <span>{s.shot_id}</span>
              <span>{s.duration_s.toFixed(1)}s</span>
            </div>
            <div className="strip__title">{s.description}</div>
          </button>
        ))}
      </div>
    </section>
  );
}

function ShotDrawer({ shot }) {
  if (!shot) return null;
  const chosen = shot.routing;
  const final = shot.final;
  return (
    <div className="section" style={{paddingTop:0}}>
      <div className="drawer" key={shot.shot_id}>
        <div>
          <p className="drawer__eyebrow">shot {shot.shot_id.replace("sh_","")} · scene {shot.scene} · {shot.duration_s.toFixed(1)}s</p>
          <h3 className="drawer__title">{shot.description}</h3>
          <blockquote className="drawer__prompt">"{shot.prompt.primary}"</blockquote>
          {shot.artistic_direction && (
            <p className="drawer__dir"><em>Direction.</em> {shot.artistic_direction}</p>
          )}

          <dl className="drawer__kv">
            <dt>provider</dt><dd>{chosen.chosen_provider}</dd>
            <dt>model</dt><dd>{chosen.chosen_model}</dd>
            <dt>rationale</dt><dd style={{fontFamily:"var(--f-serif)",fontSize:13,color:"var(--fg-2)",lineHeight:1.4}}>{chosen.rationale}</dd>
            <dt>alternates</dt><dd>{(chosen.alternates||[]).join(", ") || "—"}</dd>
          </dl>
        </div>

        <div>
          <p className="drawer__eyebrow">attempts · {shot.attempts.length}</p>
          <div className="drawer__attempts">
            {shot.attempts.map(a => (
              <div key={a.attempt_id} className="attempt">
                <span className="attempt__num">#{a.attempt_id}</span>
                <div className="attempt__body">
                  <span className="dot" style={{background: statusDotColor(a.outcome)}} />
                  <strong style={{fontWeight:600}}>{a.outcome}</strong>
                  <em> — {a.judge_notes}</em>
                </div>
                <span className="attempt__cost">${a.cost_usd.toFixed(2)} · {a.latency_s}s</span>
              </div>
            ))}
          </div>

          {final && (
            <dl className="drawer__kv" style={{marginTop:"var(--sp-3)"}}>
              <dt>final</dt><dd>{final.render_path.split("/").pop()} · attempt #{final.attempt_id}</dd>
              <dt>status</dt><dd>
                <span className="dot" style={{background: statusDotColor(shot.status)}} />
                {shot.status}
              </dd>
            </dl>
          )}
          {!final && (
            <dl className="drawer__kv" style={{marginTop:"var(--sp-3)"}}>
              <dt>status</dt><dd>
                <span className="dot" style={{background: statusDotColor(shot.status)}} />
                {shot.status} — editor will crop wider in post
              </dd>
            </dl>
          )}
        </div>
      </div>
    </div>
  );
}

/* --- Trace ----------------------------------------------------------- */
function AgentTrace({ events, shots }) {
  const [filter, setFilter] = useState("all");
  const filters = useMemo(() => {
    const agents = Array.from(new Set(events.map(e => e.agent)));
    return ["all", ...agents];
  }, [events]);

  const visible = events.filter(e => filter === "all" || e.agent === filter);
  const PAGE = 15;
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [filter]);
  const totalPages = Math.max(1, Math.ceil(visible.length / PAGE));
  const pageRows = visible.slice((page-1)*PAGE, page*PAGE);

  return (
    <section className="section" id="trace">
      <p className="section__eyebrow">§ 04 — the agent trace</p>
      <h2 className="section__title">Who did what, when.</h2>
      <div className="trace__filters">
        {filters.map(f => (
          <button key={f} className={`trace__filter${filter===f ? " trace__filter--on":""}`}
                  onClick={() => setFilter(f)}>{f.replace(/_/g," ")}</button>
        ))}
      </div>
      <div>
        {pageRows.map(e => (
          <div key={e.id} className="trace__row">
            <span className="trace__ts">{e.ts.slice(5,10)} · {e.ts.slice(11,19)}</span>
            <span className="trace__agent">
              <span className="dot" style={{background: statusDotColor(e.status==="ok" ? "approved" : "pending")}} />
              {e.agent}
            </span>
            <span className="trace__detail">
              {traceDetail(e, shots)}
            </span>
            <span className="trace__cost">
              {e.cost_usd > 0 ? `$${e.cost_usd.toFixed(2)}` : "—"}
            </span>
          </div>
        ))}
      </div>
      <nav className="trace__pager" aria-label="Trace pagination">
        <button className="trace__pagebtn" disabled={page<=1} onClick={() => setPage(p => Math.max(1, p-1))}>← prev</button>
        <span className="trace__pageinfo">
          page <span className="t-num">{page}</span> / <span className="t-num">{totalPages}</span>
          <span className="trace__pagedim">· {visible.length} events</span>
        </span>
        <button className="trace__pagebtn" disabled={page>=totalPages} onClick={() => setPage(p => Math.min(totalPages, p+1))}>next →</button>
      </nav>
    </section>
  );
}

function traceDetail(e, shots) {
  const shot = e.shot_id ? shots.find(s => s.shot_id === e.shot_id) : null;
  const who = shot ? ` · ${shot.shot_id}` : "";
  const type = (e.event_type || "call").replace(/_/g, " ");
  if (e.error) return `${type}${who} — ${e.error}`;
  return `${type}${who} · ${e.latency_s}s · ${(e.input_tokens||0)}/${(e.output_tokens||0)} tok`;
}

/* --- Ledger ---------------------------------------------------------- */
function Ledger({ events, budget }) {
  const byProvider = useMemo(() => {
    const agg = {};
    events.forEach(e => {
      const p = e.provider || "unknown";
      if (!agg[p]) agg[p] = { calls: 0, usd: 0, latency: 0 };
      agg[p].calls++;
      agg[p].usd += e.cost_usd || 0;
      agg[p].latency += e.latency_s || 0;
    });
    return Object.entries(agg).map(([p, v]) => ({ provider: p, ...v }));
  }, [events]);

  const totalUsd = byProvider.reduce((s, r) => s + r.usd, 0);
  const totalLat = byProvider.reduce((s, r) => s + r.latency, 0);
  const totalCalls = byProvider.reduce((s, r) => s + r.calls, 0);

  return (
    <section className="section" id="ledger">
      <p className="section__eyebrow">§ 05 — the production ledger</p>
      <h2 className="section__title">Receipts.</h2>
      <table className="ledger">
        <thead>
          <tr><th>provider</th><th>calls</th><th className="n">usd</th><th className="n">latency</th></tr>
        </thead>
        <tbody>
          {byProvider.map(r => (
            <tr key={r.provider}>
              <td>{r.provider}</td>
              <td>{r.calls}</td>
              <td className="n">${r.usd.toFixed(2)}</td>
              <td className="n">{r.latency.toFixed(0)}s</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td>total</td>
            <td>{totalCalls}</td>
            <td className="n">${totalUsd.toFixed(2)}</td>
            <td className="n">{totalLat.toFixed(0)}s</td>
          </tr>
        </tfoot>
      </table>

      <div style={{marginTop:"var(--sp-4)", display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:"var(--gutter)", fontFamily:"var(--f-mono)", fontSize:12}}>
        <div>
          <div style={{color:"var(--fg-3)", fontSize:10, letterSpacing:".14em", textTransform:"uppercase", marginBottom:4}}>cap</div>
          <div style={{fontSize:14}}>${budget.cap_usd.toFixed(2)} · spent ${budget.spent_usd.toFixed(2)}</div>
        </div>
        <div>
          <div style={{color:"var(--fg-3)", fontSize:10, letterSpacing:".14em", textTransform:"uppercase", marginBottom:4}}>alibaba quota</div>
          <div style={{fontSize:14}}>{budget.alibaba_quota_remaining} calls remaining</div>
        </div>
        <div>
          <div style={{color:"var(--fg-3)", fontSize:10, letterSpacing:".14em", textTransform:"uppercase", marginBottom:4}}>elevenlabs</div>
          <div style={{fontSize:14}}>{budget.elevenlabs_credits_remaining.toLocaleString()} credits</div>
        </div>
      </div>
    </section>
  );
}

/* --- Colophon -------------------------------------------------------- */
function Colophon() {
  return (
    <section className="section colophon" id="colophon">
      <p className="section__eyebrow">§ 06 — colophon</p>
      <h2 className="section__title">How it was made.</h2>
      <div className="colophon__body">
        <p>
          This film was produced by <em>rectoverso</em>, a multi-agent pipeline orchestrated by
          Anthropic&apos;s Claude Opus 4.7. A Producer agent coordinated specialists — a Screenwriter,
          a Prompt Smith, a Router, a Renderer, a Shot Judge, an Audio Agent, an Editor Agent, and a
          Creative Director — through a shared shot manifest. No human touched the frames; a human
          wrote the brief and opened the FCPXML.
        </p>
        <p style={{color:"var(--fg-2)"}}>
          Renders by Kling 2.1 Pro (human shots), Alibaba Wan 2.7 Plus (workhorse), Google Veo 3.1 Fast
          (one hero moment). Voice and SFX by ElevenLabs. Edit list exported as FCPXML 1.10.
        </p>
      </div>
      <div className="colophon__credits">
        brief · a. maizon &nbsp;·&nbsp; pipeline · rectoverso &nbsp;·&nbsp; models · claude-opus-4.7 ·
        kling-2.1 · wan-2.7 · veo-3.1 · elevenlabs
      </div>
    </section>
  );
}

/* --- App ------------------------------------------------------------- */
function App({ manifest, events }) {
  const [activeId, setActiveId] = useState(null);
  const activeShot = activeId ? manifest.shots.find(s => s.shot_id === activeId) : null;

  return (
    <div className="page">
      <Masthead />
      <Hero brief={manifest.brief} edit={manifest.edit} project={manifest.project_id} />
      <Brief brief={manifest.brief} />
      <Script shots={manifest.shots} />
      <ShotStrip shots={manifest.shots} activeId={activeId} onSelect={id => setActiveId(id === activeId ? null : id)} />
      {activeShot && <ShotDrawer shot={activeShot} />}
      <AgentTrace events={events} shots={manifest.shots} />
      <Ledger events={events} budget={manifest.budget} />
      <Colophon />
    </div>
  );
}

/* --- Bootstrap ------------------------------------------------------- */
(function boot() {
  const m = JSON.parse(document.getElementById("manifest-data").textContent);
  const e = JSON.parse(document.getElementById("events-data").textContent);
  ReactDOM.createRoot(document.getElementById("root")).render(
    <App manifest={m} events={e} />
  );
})();
