// Recto Verso pipeline animation — visual primitives
// Exports: Eyebrow, Rule, AgentNode, ManifestSpine, Edge, Caption, BriefCard, FilmFrame

const PAPER = '#F4EFE6';
const PAPER_2 = '#EBE4D6';
const INK = '#1C1A17';
const INK_2 = '#403B34';
const FG_2 = 'rgba(28, 26, 23, 0.68)';
const FG_3 = 'rgba(28, 26, 23, 0.44)';
const FG_RULE = 'rgba(28, 26, 23, 0.18)';
const FG_RULE_2 = 'rgba(28, 26, 23, 0.08)';
const ACCENT = '#C3432B';
const STATUS_APPROVED = '#4A6B3A';
const STATUS_REJECTED = '#8A3A2A';
const STATUS_ESCALATED = '#9A6A24';
const STATUS_PENDING = '#6A6354';

const F_DISPLAY = '"Gragio", Georgia, serif';
const F_SERIF = '"Pregio", Georgia, serif';
const F_MONO = '"JetBrains Mono", ui-monospace, monospace';

window.RV = { PAPER, PAPER_2, INK, INK_2, FG_2, FG_3, FG_RULE, FG_RULE_2, ACCENT,
  STATUS_APPROVED, STATUS_REJECTED, STATUS_ESCALATED, STATUS_PENDING,
  F_DISPLAY, F_SERIF, F_MONO };

// Eyebrow (small caps mono label)
function Eyebrow({ children, color = FG_2, size = 11, style = {} }) {
  return (
    <div style={{
      fontFamily: F_MONO, fontSize: size, letterSpacing: '0.12em',
      textTransform: 'uppercase', color, fontWeight: 500,
      ...style,
    }}>
      {children}
    </div>
  );
}

// Hairline rule
function Rule({ width = 100, color = FG_RULE, thickness = 1, x = 0, y = 0, opacity = 1, vertical = false }) {
  return (
    <div style={{
      position: 'absolute', left: x, top: y,
      width: vertical ? thickness : width,
      height: vertical ? width : thickness,
      background: color, opacity,
    }} />
  );
}

// Agent node — a labeled circle/disk with role index
// Shows: role number (mono), agent name (serif), tier color
function AgentNode({
  x, y, label, role, tier = 2, size = 130,
  active = false, glowing = false, dimmed = false, pulse = 0,
  caption = null,
  onCenter = false, // align coords as center vs top-left
  zIndex = 3,
}) {
  const tierColors = {
    1: { fill: '#F4EFE6', stroke: INK, label: 'Tier 1' },
    2: { fill: '#EBE4D6', stroke: INK, label: 'Tier 2' },
    3: { fill: '#F4EFE6', stroke: INK_2, label: 'Tier 3' },
    4: { fill: '#F4EFE6', stroke: INK_2, label: 'Tier 4' },
  };
  const t = tierColors[tier];
  const offset = onCenter ? -size / 2 : 0;

  const opacity = dimmed ? 0.32 : 1;
  const ringExtra = glowing ? 6 : 0;

  return (
    <div style={{
      position: 'absolute',
      left: x + offset, top: y + offset,
      width: size, height: size,
      opacity,
      zIndex,
      transition: 'opacity 200ms cubic-bezier(0.2, 0, 0, 1)',
    }}>
      {/* pulse ring */}
      {pulse > 0 && (
        <div style={{
          position: 'absolute',
          inset: -20,
          borderRadius: '50%',
          border: `1px solid ${ACCENT}`,
          opacity: (1 - pulse) * 0.7,
          transform: `scale(${1 + pulse * 0.6})`,
        }} />
      )}
      <div style={{
        width: size, height: size,
        borderRadius: '50%',
        background: t.fill,
        border: `${active ? 1.5 : 1}px solid ${active ? INK : t.stroke}`,
        boxSizing: 'border-box',
        position: 'relative',
        display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
        textAlign: 'center',
        padding: '0 12px',
      }}>
        <div style={{
          fontFamily: F_DISPLAY, fontSize: size > 130 ? 24 : 17,
          color: INK, lineHeight: 1.08,
          fontStyle: 'italic',
          textWrap: 'balance',
        }}>{label}</div>
        {glowing && (
          <div style={{
            position: 'absolute',
            inset: -ringExtra,
            borderRadius: '50%',
            border: `1px solid ${ACCENT}`,
            opacity: 0.7,
            pointerEvents: 'none',
          }} />
        )}
      </div>
      {/* role tag below the disk, not inside it */}
      <div style={{
        position: 'absolute',
        left: '50%', top: size + 8,
        transform: 'translateX(-50%)',
        fontFamily: F_MONO, fontSize: 9.5,
        color: FG_3, letterSpacing: '0.14em',
        textTransform: 'uppercase', whiteSpace: 'nowrap',
      }}>{role}{caption ? ` · ${caption}` : ''}</div>
    </div>
  );
}

// The Manifest — central spine. Cylinder/database shape, editorial.
function ManifestSpine({ x, y, width = 220, height = 320, active = false, glow = 0, zIndex = 3 }) {
  return (
    <div style={{
      position: 'absolute',
      left: x - width / 2, top: y - height / 2,
      width, height,
      zIndex,
    }}>
      {/* glow ring */}
      {glow > 0 && (
        <div style={{
          position: 'absolute', inset: -16,
          border: `1px solid ${ACCENT}`,
          opacity: glow * 0.5,
          borderRadius: 4,
        }} />
      )}
      <div style={{
        width: '100%', height: '100%',
        background: PAPER,
        border: `1px solid ${INK}`,
        position: 'relative',
        display: 'flex', flexDirection: 'column',
        padding: '20px 22px',
      }}>
        {/* corner ticks */}
        <div style={{ position: 'absolute', top: 6, left: 6, width: 8, height: 1, background: INK }} />
        <div style={{ position: 'absolute', top: 6, left: 6, width: 1, height: 8, background: INK }} />
        <div style={{ position: 'absolute', top: 6, right: 6, width: 8, height: 1, background: INK }} />
        <div style={{ position: 'absolute', top: 6, right: 6, width: 1, height: 8, background: INK }} />
        <div style={{ position: 'absolute', bottom: 6, left: 6, width: 8, height: 1, background: INK }} />
        <div style={{ position: 'absolute', bottom: 6, left: 6, width: 1, height: 8, background: INK }} />
        <div style={{ position: 'absolute', bottom: 6, right: 6, width: 8, height: 1, background: INK }} />
        <div style={{ position: 'absolute', bottom: 6, right: 6, width: 1, height: 8, background: INK }} />

        <div style={{
          fontFamily: F_MONO, fontSize: 10, letterSpacing: '0.16em',
          color: FG_2, textTransform: 'uppercase',
          textAlign: 'center', marginBottom: 6,
        }}>The</div>
        <div style={{
          fontFamily: F_DISPLAY, fontSize: 36,
          color: INK, fontStyle: 'italic',
          textAlign: 'center', lineHeight: 1,
          marginBottom: 18,
        }}>Manifest</div>

        <div style={{ borderTop: `1px solid ${FG_RULE}`, margin: '0 -8px 14px' }} />

        {/* Entity rows */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {[
            ['shot', '12'], ['attempt', '34'], ['final', '12'],
            ['judge_feedback', '18'], ['creative_decision', '4'],
            ['audio_dialogue', '8'], ['edit', '1'], ['budget', '$ 38.40'],
          ].map(([k, v]) => (
            <div key={k} style={{
              display: 'flex', justifyContent: 'space-between',
              fontFamily: F_MONO, fontSize: 10,
              color: FG_2, letterSpacing: '0.04em',
              fontVariantNumeric: 'tabular-nums',
            }}>
              <span>{k}</span>
              <span style={{ color: INK }}>{v}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// Edge — a line between two points, with optional progress reveal,
// label, dashed/solid, color, arrowhead.
//
// Coordinates are in stage space (absolute). progress: 0..1.
function Edge({
  from, to,
  progress = 1, // 0..1: reveal head along the line
  fadeProgress = 1, // 0..1: opacity ramp
  color = INK,
  thickness = 1,
  dashed = false,
  label = null,
  labelOffset = -14,
  arrow = true,
  curve = 0, // pixels of bow perpendicular to the line
}) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.sqrt(dx * dx + dy * dy);
  if (len < 0.01) return null;

  // SVG box covers both endpoints; pad for arrowhead and curve.
  const pad = 30;
  const minX = Math.min(from.x, to.x) - pad;
  const minY = Math.min(from.y, to.y) - pad - Math.abs(curve);
  const maxX = Math.max(from.x, to.x) + pad;
  const maxY = Math.max(from.y, to.y) + pad + Math.abs(curve);
  const w = maxX - minX;
  const h = maxY - minY;

  const fx = from.x - minX;
  const fy = from.y - minY;
  const tx = to.x - minX;
  const ty = to.y - minY;

  // Control point for quadratic bezier
  const mx = (fx + tx) / 2;
  const my = (fy + ty) / 2;
  const nx = -dy / len; // perpendicular
  const ny = dx / len;
  const cx = mx + nx * curve;
  const cy = my + ny * curve;

  // Approximate path length for dasharray reveal
  const pathLen = curve === 0 ? len : (len + Math.abs(curve) * 0.8);
  const drawn = pathLen * progress;

  // Arrow position along bezier at progress
  // Use simple linear interpolation for non-curved; for curved, sample bezier.
  const sampleBezier = (t) => {
    const x = (1 - t) * (1 - t) * fx + 2 * (1 - t) * t * cx + t * t * tx;
    const y = (1 - t) * (1 - t) * fy + 2 * (1 - t) * t * cy + t * t * ty;
    // tangent
    const tanX = 2 * (1 - t) * (cx - fx) + 2 * t * (tx - cx);
    const tanY = 2 * (1 - t) * (cy - fy) + 2 * t * (ty - cy);
    return { x, y, tanX, tanY };
  };
  const tipT = clamp(progress, 0, 1);
  const tip = sampleBezier(tipT);
  const tipAngle = Math.atan2(tip.tanY, tip.tanX) * 180 / Math.PI;

  const pathD = `M ${fx} ${fy} Q ${cx} ${cy} ${tx} ${ty}`;

  const labelMid = sampleBezier(0.5);
  const showLabel = label && progress > 0.5;

  return (
    <div style={{
      position: 'absolute',
      left: minX, top: minY,
      width: w, height: h,
      opacity: fadeProgress,
      pointerEvents: 'none',
      zIndex: 1,
    }}>
      <svg width={w} height={h} style={{ position: 'absolute', inset: 0, overflow: 'visible' }}>
        <path
          d={pathD}
          fill="none"
          stroke={color}
          strokeWidth={thickness}
          strokeLinecap="round"
          strokeDasharray={dashed ? '4 5' : (pathLen + ' ' + pathLen)}
          strokeDashoffset={dashed ? 0 : (pathLen - drawn)}
        />
        {arrow && progress > 0.05 && (
          <g transform={`translate(${tip.x} ${tip.y}) rotate(${tipAngle})`}>
            <path d="M 0 0 L -8 -4 L -6 0 L -8 4 Z" fill={color} />
          </g>
        )}
      </svg>
      {showLabel && (
        <div style={{
          position: 'absolute',
          left: labelMid.x, top: labelMid.y + labelOffset,
          transform: 'translate(-50%, -50%)',
          fontFamily: F_MONO, fontSize: 9,
          letterSpacing: '0.1em', textTransform: 'uppercase',
          color, background: PAPER,
          padding: '2px 6px',
          whiteSpace: 'nowrap',
          fontWeight: 500,
        }}>{label}</div>
      )}
    </div>
  );
}

// A token traveling along the line — small dot for "data" flow
function FlowToken({ from, to, t = 0, size = 6, color = ACCENT, curve = 0 }) {
  if (t < 0 || t > 1) return null;
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.sqrt(dx * dx + dy * dy);
  const nx = -dy / len;
  const ny = dx / len;
  const mx = (from.x + to.x) / 2 + nx * curve;
  const my = (from.y + to.y) / 2 + ny * curve;
  const x = (1 - t) * (1 - t) * from.x + 2 * (1 - t) * t * mx + t * t * to.x;
  const y = (1 - t) * (1 - t) * from.y + 2 * (1 - t) * t * my + t * t * to.y;
  return (
    <div style={{
      position: 'absolute',
      left: x - size / 2, top: y - size / 2,
      width: size, height: size,
      borderRadius: '50%',
      background: color,
    }} />
  );
}

// Caption block — editorial bottom-of-frame text
function Caption({ eyebrow, title, body, x = 80, y = 880, width = 560 }) {
  return (
    <div style={{
      position: 'absolute',
      left: x, top: y, width,
    }}>
      {eyebrow && <Eyebrow style={{ marginBottom: 10, color: ACCENT }}>{eyebrow}</Eyebrow>}
      {title && <div style={{
        fontFamily: F_DISPLAY, fontSize: 36,
        color: INK, lineHeight: 1.1, fontStyle: 'italic',
        marginBottom: 10, whiteSpace: 'pre-line',
      }}>{title}</div>}
      {body && <div style={{
        fontFamily: F_SERIF, fontSize: 17,
        color: INK_2, lineHeight: 1.45,
        maxWidth: 520,
      }}>{body}</div>}
    </div>
  );
}

// Brief card — the human input
function BriefCard({ x, y, width = 360, scale = 1 }) {
  return (
    <div style={{
      position: 'absolute',
      left: x - width / 2, top: y,
      width,
      transform: `scale(${scale})`,
      transformOrigin: 'top center',
    }}>
      <div style={{
        background: PAPER,
        border: `1px solid ${INK}`,
        padding: '22px 26px 26px',
        position: 'relative',
      }}>
        <Eyebrow style={{ marginBottom: 8 }}>Human brief — 2026-04-22</Eyebrow>
        <div style={{ borderTop: `1px solid ${FG_RULE}`, margin: '8px 0 14px' }} />
        <div style={{
          fontFamily: F_DISPLAY, fontSize: 22,
          color: INK, fontStyle: 'italic',
          lineHeight: 1.18, marginBottom: 12,
        }}>"Magic doors appear in cities worldwide. Each leads somewhere surprising."</div>
        <div style={{
          fontFamily: F_SERIF, fontSize: 13,
          color: FG_2, lineHeight: 1.5,
        }}>30-60 seconds. The door should feel ordinary, not magical. It is just there.</div>
      </div>
    </div>
  );
}

// Film frame — final shot thumbnail with colour or image
function FilmFrame({ x, y, width = 340, height = 191, color = '#5b6f4f', image = null, label = '01', scene = 'glacier door' }) {
  return (
    <div style={{
      position: 'absolute',
      left: x, top: y, width, height,
      background: image ? '#1a1814' : color,
      overflow: 'hidden',
    }}>
      {image ? (
        <img src={image} alt={scene} style={{
          width: '100%', height: '100%', objectFit: 'cover', display: 'block',
        }} />
      ) : (
        <div style={{
          position: 'absolute',
          left: '50%', top: '20%',
          transform: 'translateX(-50%)',
          width: '20%', height: '60%',
          background: 'rgba(20,18,15,0.55)',
          borderTop: '1px solid rgba(244,239,230,0.3)',
          borderLeft: '1px solid rgba(244,239,230,0.3)',
          borderRight: '1px solid rgba(244,239,230,0.3)',
        }} />
      )}
      {/* caption strip — semi-opaque ink bar so type reads on any image */}
      <div style={{
        position: 'absolute',
        left: 0, right: 0, bottom: 0,
        padding: '10px 12px 8px',
        background: 'linear-gradient(to top, rgba(20,18,15,0.78), rgba(20,18,15,0))',
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10, color: PAPER,
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
      }}>shot {label} — {scene}</div>
    </div>
  );
}

// Tier lane — labeled vertical group
function TierLane({ x, y, width, height, label, sublabel }) {
  return (
    <div style={{
      position: 'absolute',
      left: x, top: y, width, height,
      pointerEvents: 'none',
    }}>
      <div style={{
        position: 'absolute',
        top: -28, left: 0,
        fontFamily: F_MONO, fontSize: 10,
        letterSpacing: '0.16em', textTransform: 'uppercase',
        color: FG_2,
      }}>{label}{sublabel ? <span style={{ color: FG_3, marginLeft: 8 }}>{sublabel}</span> : null}</div>
      {/* vertical hairlines */}
      <div style={{
        position: 'absolute', left: -10, top: -8,
        width: 6, height: 1, background: FG_RULE,
      }} />
      <div style={{
        position: 'absolute', left: -10, top: -8,
        width: 1, height: 12, background: FG_RULE,
      }} />
      <div style={{
        position: 'absolute', right: -10, top: -8,
        width: 6, height: 1, background: FG_RULE,
      }} />
      <div style={{
        position: 'absolute', right: -10, top: -8,
        width: 1, height: 12, background: FG_RULE,
      }} />
    </div>
  );
}

Object.assign(window, {
  Eyebrow, Rule, AgentNode, ManifestSpine, Edge, FlowToken, Caption,
  BriefCard, FilmFrame, TierLane,
});
