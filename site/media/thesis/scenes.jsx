// Scenes for the Recto Verso pipeline animation.
// Stage: 1920 x 1080. Total duration ~46s.
//
// Timeline (seconds):
//   0.0 – 4.5   Scene 1: Title + brief arrival
//   4.5 – 10.5  Scene 2: Producer dispatch fan-out
//  10.5 – 18.0  Scene 3: Manifest spine + writes/reads
//  18.0 – 28.0  Scene 4: Contracts (BLOCK / WARN tension)
//  28.0 – 36.0  Scene 5: Convergence + Editor assembly
//  36.0 – 46.0  Scene 6: Final film + provenance
//
// Stage coordinates are absolute. Layout:
//   Tier 1 (Producer) at left.
//   Manifest at center.
//   Tier 2 (Judge, CD, Audio, Editor) above and below center.
//   Tier 3 (Screenwriter, Prompt Smith) at upper right.
//   Tier 4 (Renderer) at right.

const POS = {
  // Tier 1 — Producer
  P:  { x: 240, y: 540 },

  // Manifest center
  M:  { x: 960, y: 540 },

  // Tier 2 (around the manifest)
  SJ: { x: 760, y: 250 },   // Shot Judge — top left of manifest
  CD: { x: 1160, y: 250 },  // Creative Director — top right
  AA: { x: 760, y: 830 },   // Audio Agent — bottom left
  EA: { x: 1160, y: 830 },  // Editor Agent — bottom right

  // Tier 3
  SW: { x: 1500, y: 320 },  // Screenwriter
  PS: { x: 1500, y: 540 },  // Prompt Smith

  // Tier 4
  R:  { x: 1720, y: 540 },  // Renderer
};

window.POS = POS;

// ── Helpers ─────────────────────────────────────────────────────────────────

const t01 = (lt, a, b) => clamp((lt - a) / (b - a), 0, 1);
const ease = Easing.easeInOutCubic;
const easeOut = Easing.easeOutCubic;

// Reveal-along-line: starts dimming then drawing then settling
function dispatchEdge({ from, to, start, drawDur = 0.55, color = RV.INK_2, dashed = false, label, curve = 0, hold = 99 }) {
  const { localTime } = useSprite();
  if (localTime < start) return null;
  const t = localTime - start;
  if (t > hold + drawDur + 0.4) return null;
  const progress = t01(t, 0, drawDur);
  const fadeOut = t > hold + drawDur ? clamp(1 - (t - hold - drawDur) / 0.4, 0, 1) : 1;
  return <Edge from={from} to={to} progress={progress} fadeProgress={fadeOut} color={color} dashed={dashed} label={label} curve={curve} />;
}

// ── Scene 1: Title + brief ──────────────────────────────────────────────────

function Scene1Title() {
  return (
    <Sprite start={0} end={4.6}>
      {({ localTime }) => {
        const titleOpacity = clamp(t01(localTime, 0.2, 1.2), 0, 1) * (1 - clamp(t01(localTime, 3.8, 4.6), 0, 1));
        const eyebrowOpacity = clamp(t01(localTime, 0.0, 0.6), 0, 1) * (1 - clamp(t01(localTime, 3.8, 4.6), 0, 1));
        const subOpacity = clamp(t01(localTime, 0.9, 1.6), 0, 1) * (1 - clamp(t01(localTime, 3.8, 4.6), 0, 1));
        const ruleOpacity = clamp(t01(localTime, 0.4, 1.4), 0, 1) * (1 - clamp(t01(localTime, 3.8, 4.6), 0, 1));
        const ruleWidth = ease(clamp(t01(localTime, 0.3, 1.6), 0, 1));

        // gentle drift up
        const drift = -localTime * 4;

        return (
          <div style={{ position: 'absolute', inset: 0 }}>
            <div style={{
              position: 'absolute',
              left: 0, right: 0, top: 380 + drift,
              textAlign: 'center',
              opacity: eyebrowOpacity,
            }}>
              <Eyebrow style={{ color: RV.ACCENT, fontSize: 13 }}>Recto Verso Productions — diagram no. 01</Eyebrow>
            </div>
            <div style={{
              position: 'absolute',
              left: '50%', top: 440 + drift,
              transform: 'translateX(-50%)',
              width: 1100 * ruleWidth,
              height: 1, background: RV.INK,
              opacity: ruleOpacity,
            }} />
            <div style={{
              position: 'absolute',
              left: 0, right: 0, top: 470 + drift,
              textAlign: 'center',
              fontFamily: RV.F_DISPLAY, fontStyle: 'italic',
              fontSize: 92, color: RV.INK,
              lineHeight: 1.05,
              opacity: titleOpacity,
              letterSpacing: '-0.015em',
            }}>The multi-agent pipeline</div>
            <div style={{
              position: 'absolute',
              left: 0, right: 0, top: 600 + drift,
              textAlign: 'center',
              fontFamily: RV.F_SERIF, fontSize: 22,
              color: RV.FG_2, fontStyle: 'italic',
              opacity: subOpacity,
            }}>How a brief becomes a film, and the receipts it leaves behind.</div>

            <div style={{
              position: 'absolute',
              left: 80, top: 1000,
              opacity: subOpacity,
            }}>
              <Eyebrow style={{ color: RV.FG_3 }}>00 — overture</Eyebrow>
            </div>
            <div style={{
              position: 'absolute',
              right: 80, top: 1000,
              opacity: subOpacity,
              fontFamily: RV.F_MONO, fontSize: 11,
              color: RV.FG_3, letterSpacing: '0.12em',
              textTransform: 'uppercase',
            }}>brief on one side  ·  film on the other</div>
          </div>
        );
      }}
    </Sprite>
  );
}

// ── Scene 2: Brief lands, Producer appears, fan-out ─────────────────────────

function Scene2Dispatch() {
  return (
    <Sprite start={4.5} end={10.8}>
      {({ localTime }) => {
        // Brief drops in from above (4.5–6.0), shrinks to a token that flies into Producer.
        const briefArrive = clamp(t01(localTime, 0.0, 0.9), 0, 1);
        const briefHold = localTime < 1.6;
        const briefShrink = clamp(t01(localTime, 1.6, 2.4), 0, 1);

        const briefY = -200 + easeOut(briefArrive) * 320;
        const briefScale = 1 - briefShrink * 0.7;
        const briefOpacity = 1 - briefShrink;

        // Producer enters at ~1.8s (after brief lands)
        const pIn = clamp(t01(localTime, 1.8, 2.6), 0, 1);
        // Producer pulse when receives
        const pPulse = clamp(t01(localTime, 2.3, 3.3), 0, 1);

        // Fan out edges starting 3.0s, staggered
        const targets = [
          { key: 'SW', delay: 0.0, label: 'plan brief' },
          { key: 'PS', delay: 0.15, label: '' },
          { key: 'R',  delay: 0.30, label: '' },
          { key: 'SJ', delay: 0.45, label: '' },
          { key: 'CD', delay: 0.55, label: '' },
          { key: 'AA', delay: 0.65, label: '' },
          { key: 'EA', delay: 0.75, label: '' },
        ];
        const fanStart = 3.0;
        const drawDur = 0.6;

        // captions
        const cap1Opacity = clamp(t01(localTime, 0.2, 1.0), 0, 1) * (1 - clamp(t01(localTime, 2.6, 3.4), 0, 1));
        const cap2Opacity = clamp(t01(localTime, 3.4, 4.2), 0, 1) * (1 - clamp(t01(localTime, 5.6, 6.3), 0, 1));

        return (
          <div style={{ position: 'absolute', inset: 0 }}>
            {/* the falling brief card */}
            {briefOpacity > 0.01 && (
              <div style={{
                position: 'absolute',
                left: '50%', top: briefY,
                transform: `translateX(-50%) scale(${briefScale})`,
                transformOrigin: 'top center',
                opacity: briefOpacity,
              }}>
                <BriefCard x={0} y={0} width={400} />
              </div>
            )}

            {/* Producer appearing */}
            {pIn > 0 && (
              <AgentNode
                x={POS.P.x} y={POS.P.y}
                role="01 / Tier 1"
                label="Producer"
                tier={1}
                size={150}
                onCenter
                glowing={localTime > 2.3 && localTime < 3.2}
                pulse={pPulse < 1 ? pPulse : 0}
                caption="orchestrator"
              />
            )}

            {/* Fan-out targets — appear as the edges arrive */}
            {targets.map(({ key, delay }) => {
              const arrive = clamp(t01(localTime, fanStart + delay + drawDur * 0.4, fanStart + delay + drawDur * 0.95), 0, 1);
              if (arrive <= 0) return null;
              const tier = key === 'SW' || key === 'PS' ? 3 : key === 'R' ? 4 : 2;
              const labels = {
                SW: 'Screenwriter', PS: 'Prompt Smith', R: 'Renderer',
                SJ: 'Shot Judge', CD: 'Creative Director',
                AA: 'Audio', EA: 'Editor',
              };
              const roleNums = {
                SW: '02 / Tier 3', PS: '03 / Tier 3', R: '04 / Tier 4',
                SJ: '05 / Tier 2', CD: '06 / Tier 2',
                AA: '07 / Tier 2', EA: '08 / Tier 2',
              };
              return (
                <div key={key} style={{ opacity: arrive }}>
                  <AgentNode
                    x={POS[key].x} y={POS[key].y}
                    role={roleNums[key]}
                    label={labels[key]}
                    tier={tier}
                    size={110}
                    onCenter
                  />
                </div>
              );
            })}

            {/* Edges from Producer to targets */}
            {targets.map(({ key, delay }) => {
              const t = localTime - (fanStart + delay);
              if (t < 0) return null;
              const progress = clamp(t / drawDur, 0, 1);
              return (
                <Edge
                  key={key}
                  from={POS.P} to={POS[key]}
                  progress={progress}
                  color={RV.INK_2}
                  thickness={1}
                />
              );
            })}

            {/* Captions */}
            {cap1Opacity > 0.01 && (
              <div style={{ opacity: cap1Opacity }}>
                <Caption
                  eyebrow="01 / Brief"
                  title="A human writes the brief."
                  body="A logline, a duration, a few notes on tone. The pipeline never starts itself — the camera is held by the Producer, but the first instruction is always human."
                  x={120} y={780} width={540}
                />
              </div>
            )}
            {cap2Opacity > 0.01 && (
              <div style={{ opacity: cap2Opacity }}>
                <Caption
                  eyebrow="02 / Dispatch"
                  title="The Producer is the only voice that issues work."
                  body="Seven specialists wait. The Producer speaks; they answer. Nothing in this pipeline calls another agent directly."
                  x={120} y={780} width={540}
                />
              </div>
            )}
          </div>
        );
      }}
    </Sprite>
  );
}

// ── Scene 3: Manifest spine ─────────────────────────────────────────────────

function Scene3Manifest() {
  return (
    <Sprite start={10.5} end={18.2}>
      {({ localTime }) => {
        // All seven agents persist in their Scene 2 positions.
        // Manifest fades in at center, replacing the empty space.
        // Then each agent writes a row to the manifest.

        const mIn = clamp(t01(localTime, 0.2, 1.4), 0, 1);

        // Producer stays. Edges from scene 2 fade.
        const oldEdgeFade = 1 - clamp(t01(localTime, 0.0, 1.2), 0, 1);

        // Bidirectional edges between manifest and all agents
        const writers = [
          { key: 'SW', at: 1.4 },
          { key: 'PS', at: 1.7 },
          { key: 'R',  at: 2.0 },
          { key: 'SJ', at: 2.3 },
          { key: 'CD', at: 2.6 },
          { key: 'AA', at: 2.9 },
          { key: 'EA', at: 3.2 },
          { key: 'P',  at: 3.5 },
        ];
        const drawDur = 0.5;

        const cap1Opacity = clamp(t01(localTime, 0.6, 1.4), 0, 1) * (1 - clamp(t01(localTime, 4.4, 5.2), 0, 1));
        const cap2Opacity = clamp(t01(localTime, 5.2, 6.0), 0, 1) * (1 - clamp(t01(localTime, 7.0, 7.7), 0, 1));

        return (
          <div style={{ position: 'absolute', inset: 0 }}>
            {/* Persistent agents */}
            <AgentNode x={POS.P.x} y={POS.P.y} role="01 / Tier 1" label="Producer" tier={1} size={150} onCenter caption="orchestrator" />
            <AgentNode x={POS.SW.x} y={POS.SW.y} role="02 / Tier 3" label="Screenwriter" tier={3} size={110} onCenter />
            <AgentNode x={POS.PS.x} y={POS.PS.y} role="03 / Tier 3" label="Prompt Smith" tier={3} size={110} onCenter />
            <AgentNode x={POS.R.x}  y={POS.R.y}  role="04 / Tier 4" label="Renderer" tier={4} size={110} onCenter />
            <AgentNode x={POS.SJ.x} y={POS.SJ.y} role="05 / Tier 2" label="Shot Judge" tier={2} size={110} onCenter />
            <AgentNode x={POS.CD.x} y={POS.CD.y} role="06 / Tier 2" label="Creative Director" tier={2} size={110} onCenter />
            <AgentNode x={POS.AA.x} y={POS.AA.y} role="07 / Tier 2" label="Audio" tier={2} size={110} onCenter />
            <AgentNode x={POS.EA.x} y={POS.EA.y} role="08 / Tier 2" label="Editor" tier={2} size={110} onCenter />

            {/* Manifest spine */}
            <div style={{ opacity: mIn }}>
              <ManifestSpine x={POS.M.x} y={POS.M.y} width={240} height={340} glow={mIn < 0.95 ? mIn : 0} />
            </div>

            {/* Old producer→target edges fade out */}
            {oldEdgeFade > 0.01 && (
              <div style={{ opacity: oldEdgeFade * 0.4 }}>
                {['SW','PS','R','SJ','CD','AA','EA'].map(k => (
                  <Edge key={k} from={POS.P} to={POS[k]} progress={1} color={RV.INK_2} />
                ))}
              </div>
            )}

            {/* Writes — bidirectional edges to manifest */}
            {writers.map(({ key, at }) => {
              const t = localTime - at;
              if (t < 0) return null;
              const progress = clamp(t / drawDur, 0, 1);
              const start = POS[key];
              const end = POS.M;
              // Endpoint nudge so edges don't pierce into the manifest box
              const dx = end.x - start.x;
              const dy = end.y - start.y;
              const len = Math.sqrt(dx * dx + dy * dy);
              const ux = dx / len, uy = dy / len;
              const halfW = 120; // half manifest width plus margin
              const halfH = 170;
              // approximate stop on rectangle edge
              const tx = end.x - ux * halfW;
              const ty = end.y - uy * Math.min(halfH, halfW);
              return (
                <Edge
                  key={key}
                  from={start} to={{ x: tx, y: ty }}
                  progress={progress}
                  color={RV.INK_2}
                  thickness={1}
                  arrow={false}
                />
              );
            })}

            {/* Tokens flowing into manifest */}
            {writers.map(({ key, at }) => {
              const t = localTime - at;
              if (t < 0.1 || t > drawDur + 0.4) return null;
              const tt = clamp((t - 0.1) / 0.5, 0, 1);
              return (
                <FlowToken
                  key={key}
                  from={POS[key]} to={POS.M}
                  t={tt}
                  color={RV.ACCENT}
                  size={5}
                />
              );
            })}

            {/* Captions */}
            {cap1Opacity > 0.01 && (
              <div style={{ opacity: cap1Opacity }}>
                <Caption
                  eyebrow="03 / Manifest"
                  title={<>No agent talks to another.<br />They write to the page.</>}
                  body="Every shot, every attempt, every judge note, every dollar — landed as a row in a single ledger. The Manifest is the only shared memory."
                  x={120} y={830} width={620}
                />
              </div>
            )}
            {cap2Opacity > 0.01 && (
              <div style={{ opacity: cap2Opacity }}>
                <Caption
                  eyebrow="03 / Manifest"
                  title="The page reads in both directions."
                  body="Anyone can write a row; anyone can read one. Coordination becomes archive — the production is its own paper trail."
                  x={120} y={830} width={620}
                />
              </div>
            )}
          </div>
        );
      }}
    </Sprite>
  );
}

// ── Scene 4: Contracts (BLOCK / WARN tension) ───────────────────────────────

function Scene4Contracts() {
  return (
    <Sprite start={18.0} end={28.5}>
      {({ localTime }) => {
        // Manifest dims to background. Highlight specific contracts:
        //  t≈0.5  Renderer pushes to manifest, Judge reads → marks REJECTED
        //  t≈3.0  Judge → Prompt Smith (BLOCK: judge_to_prompt) — solid red
        //  t≈5.0  CD writes decision; CD → Prompt Smith (BLOCK) — solid red
        //  t≈7.0  Audio …→ Editor (WARN: audio_to_editor) — dashed amber
        //  t≈8.5  CD → Editor (BLOCK: cd_authority_pre_editor)
        // Caption at bottom rotates with the active contract.

        // Dim every persistent edge from scene 3 except active actors
        const baseDim = 0.35;

        const events = [
          { at: 0.4, type: 'render',  who: ['R'] },
          { at: 1.2, type: 'judge',   who: ['SJ'] },
          { at: 3.0, type: 'block',   from: 'SJ', to: 'PS', label: 'BLOCK · judge_to_prompt', dashed: false },
          { at: 5.0, type: 'block',   from: 'CD', to: 'PS', label: 'BLOCK · cd_to_prompt', dashed: false, curve: -40 },
          { at: 7.0, type: 'warn',    from: 'AA', to: 'EA', label: 'WARN · audio_to_editor', dashed: true },
          { at: 8.5, type: 'block',   from: 'CD', to: 'EA', label: 'BLOCK · cd_authority', dashed: false, curve: 40 },
        ];

        // Determine active actors at this time for highlighting
        const activeAt = localTime;

        // node states
        const states = {};
        ['P','SJ','CD','AA','EA','SW','PS','R'].forEach(k => states[k] = { highlight: false, color: null });

        // Renderer pulse t=0.4
        if (activeAt > 0.4 && activeAt < 1.5) states.R.highlight = true;
        if (activeAt > 1.0 && activeAt < 2.6) states.SJ.highlight = true;
        if (activeAt > 2.6 && activeAt < 4.4) { states.SJ.highlight = true; states.PS.highlight = true; }
        if (activeAt > 4.6 && activeAt < 6.6) { states.CD.highlight = true; states.PS.highlight = true; }
        if (activeAt > 6.6 && activeAt < 8.2) { states.AA.highlight = true; states.EA.highlight = true; }
        if (activeAt > 8.2 && activeAt < 10.0) { states.CD.highlight = true; states.EA.highlight = true; }

        const captions = [
          { at: 0.0, dur: 2.5, eyebrow: '04 / Verdict', title: 'A render lands. The Judge reads it.', body: 'Floats above ice. No contact shadow. Scale unconvincing. Rejected.' },
          { at: 2.5, dur: 2.4, eyebrow: '04 / Block', title: 'Judge → Prompt Smith.\nA blocking contract.', body: 'The verdict is not advice. It is a hard constraint, written into the next prompt before it is allowed to run.' },
          { at: 4.9, dur: 2.0, eyebrow: '04 / Block', title: 'Creative Director overrides.', body: 'A second block, the only one that can outrank the Judge — to keep the film, not just the shot, intact.' },
          { at: 6.9, dur: 1.5, eyebrow: '04 / Warn', title: 'Audio whispers to Editor.', body: 'Soft contract: a warning, not a stop. The Editor may proceed; the trace records the whisper.' },
          { at: 8.4, dur: 2.0, eyebrow: '04 / Block', title: 'Creative Director, last word before cut.', body: 'No edit ships without the CD\u2019s sign-off. Authority pre-editor. The film holds together because someone is reading the whole.' },
        ];

        const renderAgent = (key) => {
          const tier = key === 'P' ? 1 : (key === 'SW' || key === 'PS') ? 3 : key === 'R' ? 4 : 2;
          const labels = {
            P: 'Producer', SW: 'Screenwriter', PS: 'Prompt Smith', R: 'Renderer',
            SJ: 'Shot Judge', CD: 'Creative Director', AA: 'Audio', EA: 'Editor',
          };
          const roles = {
            P: '01 / Tier 1', SW: '02 / Tier 3', PS: '03 / Tier 3', R: '04 / Tier 4',
            SJ: '05 / Tier 2', CD: '06 / Tier 2', AA: '07 / Tier 2', EA: '08 / Tier 2',
          };
          const size = key === 'P' ? 150 : 110;
          const s = states[key];
          return (
            <AgentNode key={key}
              x={POS[key].x} y={POS[key].y}
              role={roles[key]} label={labels[key]} tier={tier} size={size} onCenter
              dimmed={!s.highlight && activeAt > 1.0}
              glowing={s.highlight}
            />
          );
        };

        return (
          <div style={{ position: 'absolute', inset: 0 }}>
            {/* dimmed manifest connections (the structure remains visible) */}
            <div style={{ opacity: 0.18 }}>
              {['SW','PS','R','SJ','CD','AA','EA','P'].map(k => {
                const start = POS[k];
                const end = POS.M;
                const dx = end.x - start.x;
                const dy = end.y - start.y;
                const len = Math.sqrt(dx * dx + dy * dy);
                const ux = dx / len, uy = dy / len;
                const tx = end.x - ux * 120;
                const ty = end.y - uy * 120;
                return <Edge key={k} from={start} to={{ x: tx, y: ty }} progress={1} color={RV.INK_2} arrow={false} />;
              })}
            </div>

            {/* Manifest dimmed */}
            <div style={{ opacity: 0.45 }}>
              <ManifestSpine x={POS.M.x} y={POS.M.y} width={240} height={340} />
            </div>

            {/* All agents */}
            {['P','SW','PS','R','SJ','CD','AA','EA'].map(renderAgent)}

            {/* Render → Judge token (verdict moment, t 0.4–1.2) */}
            {(() => {
              const t = activeAt - 0.4;
              if (t < 0 || t > 0.9) return null;
              return (
                <Edge
                  from={POS.R} to={POS.SJ}
                  progress={clamp(t / 0.7, 0, 1)}
                  color={RV.INK_2}
                  curve={-30}
                />
              );
            })()}

            {/* REJECTED stamp on Judge */}
            {(() => {
              if (activeAt < 1.6 || activeAt > 4.4) return null;
              const opacity = activeAt < 2.0 ? t01(activeAt, 1.6, 2.0)
                : activeAt > 4.0 ? 1 - t01(activeAt, 4.0, 4.4) : 1;
              return (
                <div style={{
                  position: 'absolute',
                  left: POS.SJ.x + 70, top: POS.SJ.y - 70,
                  fontFamily: RV.F_MONO, fontSize: 10,
                  letterSpacing: '0.16em', textTransform: 'uppercase',
                  color: RV.STATUS_REJECTED, opacity,
                  border: `1px solid ${RV.STATUS_REJECTED}`,
                  padding: '3px 8px',
                  background: RV.PAPER,
                  whiteSpace: 'nowrap',
                }}>
                  rejected · attempt 02
                </div>
              );
            })()}

            {/* The render the Judge is reading — appears upper-left during verdict */}
            {(() => {
              if (activeAt < 0.4 || activeAt > 4.6) return null;
              const opacity = activeAt < 1.0 ? t01(activeAt, 0.4, 1.0)
                : activeAt > 4.2 ? 1 - t01(activeAt, 4.2, 4.6) : 1;
              const cardX = 100, cardY = 120;
              const cardW = 360, cardH = 203;
              return (
                <div style={{ opacity }}>
                  {/* eyebrow */}
                  <div style={{
                    position: 'absolute', left: cardX, top: cardY - 28,
                    fontFamily: RV.F_MONO, fontSize: 10,
                    letterSpacing: '0.16em', textTransform: 'uppercase',
                    color: RV.FG_2,
                  }}>render · sh_006 · attempt 02</div>
                  {/* the frame */}
                  <div style={{
                    position: 'absolute', left: cardX, top: cardY,
                    width: cardW, height: cardH,
                    background: '#1a1814', overflow: 'hidden',
                  }}>
                    <img src="assets/magic-doors/sh_006.png" alt="render in question"
                         style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
                    {/* REJECTED stamp inside the frame */}
                    <div style={{
                      position: 'absolute',
                      right: 14, top: 14,
                      fontFamily: RV.F_MONO, fontSize: 11,
                      letterSpacing: '0.18em', textTransform: 'uppercase',
                      color: RV.STATUS_REJECTED,
                      border: `1px solid ${RV.STATUS_REJECTED}`,
                      padding: '4px 10px',
                      background: 'rgba(244,239,230,0.92)',
                    }}>rejected</div>
                  </div>
                  {/* judge note beneath */}
                  <div style={{
                    position: 'absolute', left: cardX, top: cardY + cardH + 14,
                    width: cardW,
                    fontFamily: RV.F_SERIF, fontSize: 13, fontStyle: 'italic',
                    color: RV.INK_2, lineHeight: 1.45,
                  }}>
                    "Floats above ice. No contact shadow. Scale unconvincing."
                    <div style={{
                      marginTop: 6,
                      fontFamily: RV.F_MONO, fontStyle: 'normal',
                      fontSize: 10, color: RV.FG_3,
                      letterSpacing: '0.14em', textTransform: 'uppercase',
                    }}>— shot judge, 13:08:00</div>
                  </div>
                </div>
              );
            })()}

            {/* Contract edges */}
            {events.filter(e => e.type === 'block' || e.type === 'warn').map((e, i) => {
              const t = activeAt - e.at;
              if (t < 0) return null;
              const drawDur = 0.6;
              const progress = clamp(t / drawDur, 0, 1);
              const fade = t > 1.8 + drawDur ? clamp(1 - (t - 1.8 - drawDur) / 0.6, 0, 1) : 1;
              const color = e.type === 'block' ? RV.ACCENT : RV.STATUS_ESCALATED;
              return (
                <Edge
                  key={i}
                  from={POS[e.from]} to={POS[e.to]}
                  progress={progress}
                  fadeProgress={fade}
                  color={color}
                  thickness={e.type === 'block' ? 1.5 : 1}
                  dashed={e.dashed}
                  label={e.label}
                  curve={e.curve || 0}
                />
              );
            })}

            {/* Caption at bottom — switch by time */}
            {captions.map((c, i) => {
              const t = activeAt - c.at;
              if (t < 0 || t > c.dur + 0.6) return null;
              const opacity = t < 0.4 ? t / 0.4 : t > c.dur ? 1 - (t - c.dur) / 0.6 : 1;
              return (
                <div key={i} style={{ opacity: clamp(opacity, 0, 1) }}>
                  <Caption eyebrow={c.eyebrow} title={c.title} body={c.body} x={120} y={830} width={680} />
                </div>
              );
            })}

            {/* Legend in upper right */}
            <div style={{
              position: 'absolute',
              right: 80, top: 60,
              opacity: clamp(t01(activeAt, 2.6, 3.4), 0, 1) * (1 - clamp(t01(activeAt, 9.5, 10.3), 0, 1)),
            }}>
              <Eyebrow style={{ marginBottom: 12 }}>Contracts</Eyebrow>
              <div style={{ borderTop: `1px solid ${RV.FG_RULE}`, marginBottom: 12, width: 200 }} />
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
                <div style={{ width: 28, height: 0, borderTop: `1.5px solid ${RV.ACCENT}` }} />
                <div style={{ fontFamily: RV.F_MONO, fontSize: 10, color: RV.INK, letterSpacing: '0.06em' }}>BLOCK</div>
                <div style={{ fontFamily: RV.F_SERIF, fontSize: 12, color: RV.FG_2, fontStyle: 'italic' }}>halts the next step</div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <div style={{ width: 28, height: 0, borderTop: `1px dashed ${RV.STATUS_ESCALATED}` }} />
                <div style={{ fontFamily: RV.F_MONO, fontSize: 10, color: RV.INK, letterSpacing: '0.06em' }}>WARN</div>
                <div style={{ fontFamily: RV.F_SERIF, fontSize: 12, color: RV.FG_2, fontStyle: 'italic' }}>logged, not blocking</div>
              </div>
            </div>
          </div>
        );
      }}
    </Sprite>
  );
}

// ── Scene 5: Convergence ────────────────────────────────────────────────────

function Scene5Convergence() {
  return (
    <Sprite start={28.3} end={36.5}>
      {({ localTime }) => {
        // The graph collapses inward. Editor pulls finalized shots from the manifest,
        // assembles a ribbon of frames at the bottom of the canvas.

        // collapse progress 0 → 1 over 0–2s
        const collapse = ease(clamp(t01(localTime, 0.0, 1.6), 0, 1));

        // Editor moves to bottom-center, manifest moves up.
        // Ribbon of shots emerges and slides
        const ribbonIn = clamp(t01(localTime, 1.6, 2.6), 0, 1);

        // Each frame appears in sequence — real Magic Doors renders
        const frames = [
          { i: 0, image: 'assets/magic-doors/sh_001.png', label: '01', scene: 'the hallway' },
          { i: 1, image: 'assets/magic-doors/sh_002.png', label: '02', scene: 'reef beyond' },
          { i: 2, image: 'assets/magic-doors/sh_003.png', label: '03', scene: 'savanna at dusk' },
          { i: 3, image: 'assets/magic-doors/sh_004.png', label: '04', scene: 'rainforest pass' },
          { i: 4, image: 'assets/magic-doors/sh_005.png', label: '05', scene: 'aurora field' },
          { i: 5, image: 'assets/magic-doors/sh_007.png', label: '06', scene: 'cottage at dawn' },
        ];

        const cap1 = clamp(t01(localTime, 0.6, 1.4), 0, 1) * (1 - clamp(t01(localTime, 5.6, 6.4), 0, 1));

        // dim factor for nodes during collapse
        const nodeOpacity = 1 - collapse * 0.85;

        // Editor "promoted" position
        const editorX = POS.EA.x - collapse * (POS.EA.x - 960);
        const editorY = POS.EA.y - collapse * (POS.EA.y - 320);

        return (
          <div style={{ position: 'absolute', inset: 0 }}>
            {/* fading agents */}
            <div style={{ opacity: nodeOpacity }}>
              <AgentNode x={POS.P.x} y={POS.P.y} role="01 / Tier 1" label="Producer" tier={1} size={150} onCenter />
              <AgentNode x={POS.SW.x} y={POS.SW.y} role="02 / Tier 3" label="Screenwriter" tier={3} size={110} onCenter />
              <AgentNode x={POS.PS.x} y={POS.PS.y} role="03 / Tier 3" label="Prompt Smith" tier={3} size={110} onCenter />
              <AgentNode x={POS.R.x} y={POS.R.y} role="04 / Tier 4" label="Renderer" tier={4} size={110} onCenter />
              <AgentNode x={POS.SJ.x} y={POS.SJ.y} role="05 / Tier 2" label="Shot Judge" tier={2} size={110} onCenter />
              <AgentNode x={POS.CD.x} y={POS.CD.y} role="06 / Tier 2" label="Creative Director" tier={2} size={110} onCenter />
              <AgentNode x={POS.AA.x} y={POS.AA.y} role="07 / Tier 2" label="Audio" tier={2} size={110} onCenter />
            </div>

            {/* Manifest fades + drifts up */}
            <div style={{ opacity: 1 - collapse * 0.5 }}>
              <ManifestSpine
                x={POS.M.x} y={POS.M.y - collapse * 100}
                width={240 - collapse * 60} height={340 - collapse * 100}
              />
            </div>

            {/* Editor promoted */}
            <AgentNode
              x={editorX} y={editorY}
              role="08 / Tier 2"
              label="Editor"
              tier={2}
              size={110 + collapse * 30}
              onCenter
              glowing={collapse > 0.7}
            />

            {/* Ribbon of finalized shots */}
            {ribbonIn > 0 && (() => {
              const ribbonY = 600;
              const startX = 240;
              const frameW = 220;
              const frameH = 124;
              const gap = 16;
              return (
                <div style={{
                  position: 'absolute', inset: 0, opacity: ribbonIn,
                }}>
                  {/* hairline rule above ribbon */}
                  <div style={{
                    position: 'absolute',
                    left: 240, top: ribbonY - 24,
                    width: frames.length * (frameW + gap) - gap,
                    height: 1, background: RV.FG_RULE,
                  }} />
                  <div style={{
                    position: 'absolute',
                    left: 240, top: ribbonY - 50,
                    fontFamily: RV.F_MONO, fontSize: 10,
                    letterSpacing: '0.16em', textTransform: 'uppercase',
                    color: RV.FG_2,
                  }}>shot strip · hyperframes export</div>

                  {frames.map(f => {
                    const appearAt = 2.6 + f.i * 0.35;
                    const t = localTime - appearAt;
                    if (t < 0) return null;
                    const op = clamp(t / 0.45, 0, 1);
                    return (
                      <div key={f.i} style={{ opacity: op }}>
                        <FilmFrame
                          x={startX + f.i * (frameW + gap)}
                          y={ribbonY}
                          width={frameW} height={frameH}
                          image={f.image}
                          color={f.color}
                          label={f.label}
                          scene={f.scene}
                        />
                      </div>
                    );
                  })}

                  {/* arrow from editor down into ribbon */}
                  {(() => {
                    const t = localTime - 2.6;
                    if (t < 0) return null;
                    const p = clamp(t / 0.6, 0, 1);
                    return (
                      <Edge
                        from={{ x: editorX, y: editorY + 60 }}
                        to={{ x: 960, y: ribbonY - 30 }}
                        progress={p}
                        color={RV.INK_2}
                      />
                    );
                  })()}
                </div>
              );
            })()}

            {/* caption */}
            {cap1 > 0.01 && (
              <div style={{ opacity: cap1 }}>
                <Caption
                  eyebrow="05 / Cut"
                  title={<>The Editor reads the final rows.<br />The film assembles.</>}
                  body={"Twelve approved attempts, in order, with the Audio agent\u2019s warnings folded in. A Hyperframes timeline drops; a human can take it from there."}
                  x={120} y={840} width={680}
                />
              </div>
            )}
          </div>
        );
      }}
    </Sprite>
  );
}

// ── Scene 6: Final film + provenance ────────────────────────────────────────

function Scene6Final() {
  return (
    <Sprite start={36.3} end={46.0}>
      {({ localTime }) => {
        // Frames slide together into one big hero frame; provenance line
        // appears beneath. End on a hairline rule + film title.

        const merge = ease(clamp(t01(localTime, 0.0, 1.4), 0, 1));
        const heroOpacity = clamp(t01(localTime, 1.0, 1.8), 0, 1);
        const titleOp = clamp(t01(localTime, 1.6, 2.4), 0, 1);
        const provOp = clamp(t01(localTime, 2.4, 3.4), 0, 1);
        const endOp = 1 - clamp(t01(localTime, 9.0, 9.7), 0, 1);

        // Camera-style ken burns on hero
        const kb = clamp(t01(localTime, 1.6, 9.0), 0, 1);
        const heroScale = 1 + kb * 0.06;

        return (
          <div style={{ position: 'absolute', inset: 0, opacity: endOp }}>

            {/* hero frame */}
            <div style={{
              position: 'absolute',
              left: 360, top: 200,
              width: 1200, height: 675,
              background: '#1a1814',
              opacity: heroOpacity,
              overflow: 'hidden',
              transform: `scale(${heroScale})`,
              transformOrigin: 'center',
            }}>
              {/* the actual hero render */}
              <img src="assets/magic-doors/sh_008.png" alt="Magic Doors — hero frame"
                   style={{
                     position: 'absolute', inset: 0,
                     width: '100%', height: '100%',
                     objectFit: 'cover', display: 'block',
                   }} />

              {/* caption strip in frame */}
              <div style={{
                position: 'absolute',
                left: 24, bottom: 20,
                fontFamily: RV.F_MONO, fontSize: 12,
                color: RV.PAPER, letterSpacing: '0.14em',
                textTransform: 'uppercase',
                opacity: 0.9,
                textShadow: '0 1px 4px rgba(0,0,0,0.5)',
              }}>hyperframes export · 47 seconds · 12 shots</div>
              <div style={{
                position: 'absolute',
                right: 24, bottom: 20,
                fontFamily: RV.F_MONO, fontSize: 12,
                color: RV.PAPER, letterSpacing: '0.14em',
                opacity: 0.9,
                textShadow: '0 1px 4px rgba(0,0,0,0.5)',
              }}>$ 38.40</div>
            </div>

            {/* hairline above hero */}
            <div style={{
              position: 'absolute',
              left: 360, top: 188,
              width: 1200 * heroOpacity,
              height: 1, background: RV.INK,
            }} />

            {/* film title + eyebrow */}
            <div style={{
              position: 'absolute',
              left: 360, top: 110,
              opacity: titleOp,
            }}>
              <Eyebrow style={{ marginBottom: 6, color: RV.ACCENT }}>Recto Verso Productions / film no. 04</Eyebrow>
              <div style={{
                fontFamily: RV.F_DISPLAY, fontStyle: 'italic',
                fontSize: 56, color: RV.INK, lineHeight: 1.05,
              }}>Magic Doors</div>
            </div>

            {/* Provenance line */}
            <div style={{
              position: 'absolute',
              left: 360, top: 905,
              right: 360,
              opacity: provOp,
            }}>
              <div style={{ borderTop: `1px solid ${RV.FG_RULE}`, marginBottom: 18 }} />
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 40 }}>
                <div>
                  <Eyebrow style={{ marginBottom: 6 }}>Produced by</Eyebrow>
                  <div style={{ fontFamily: RV.F_MONO, fontSize: 13, color: RV.INK, letterSpacing: '0.04em' }}>
                    rectoverso · Anthropic Claude Opus 4.7 · Kling · Wan · Veo · ElevenLabs
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <Eyebrow style={{ marginBottom: 6 }}>Receipts</Eyebrow>
                  <div style={{ fontFamily: RV.F_MONO, fontSize: 13, color: RV.INK, letterSpacing: '0.04em' }}>
                    34 attempts · 12 finals · 18 judge notes · 4 CD overrides
                  </div>
                </div>
              </div>
            </div>

            {/* Closing line */}
            <div style={{
              position: 'absolute',
              left: 0, right: 0, top: 990,
              textAlign: 'center',
              opacity: clamp(t01(localTime, 4.0, 5.0), 0, 1),
              fontFamily: RV.F_DISPLAY, fontStyle: 'italic',
              fontSize: 22, color: RV.FG_2,
            }}>Brief on one side. Film on the other.</div>
          </div>
        );
      }}
    </Sprite>
  );
}

// ── Root Animation ──────────────────────────────────────────────────────────

function PipelineAnimation() {
  // Update data-screen-label with current second for comments
  const time = useTime();
  React.useEffect(() => {
    const root = document.getElementById('animation-root');
    if (root) {
      const sec = Math.floor(time);
      root.setAttribute('data-screen-label', `t=${String(sec).padStart(2, '0')}s`);
    }
  }, [Math.floor(time)]);

  return (
    <div id="animation-root" style={{ position: 'absolute', inset: 0, background: RV.PAPER }} data-screen-label="t=00s">

      {/* Persistent masthead — small RV mark in upper left */}
      <div style={{
        position: 'absolute',
        left: 80, top: 56,
        zIndex: 5,
      }}>
        <div style={{
          fontFamily: RV.F_DISPLAY, fontStyle: 'italic',
          fontSize: 28, color: RV.INK, lineHeight: 1,
          marginBottom: 6,
        }}>RV</div>
        <div style={{
          fontFamily: RV.F_MONO, fontSize: 9,
          color: RV.FG_2, letterSpacing: '0.18em',
          textTransform: 'uppercase',
        }}>Recto Verso Productions</div>
      </div>

      {/* Persistent footer — diagram caption */}
      <div style={{
        position: 'absolute',
        right: 80, bottom: 36,
        zIndex: 5,
        fontFamily: RV.F_MONO, fontSize: 10,
        color: RV.FG_3, letterSpacing: '0.16em',
        textTransform: 'uppercase',
      }}>
        the multi-agent pipeline · diagram no. 01
      </div>

      <Scene1Title />
      <Scene2Dispatch />
      <Scene3Manifest />
      <Scene4Contracts />
      <Scene5Convergence />
      <Scene6Final />
    </div>
  );
}

window.PipelineAnimation = PipelineAnimation;
