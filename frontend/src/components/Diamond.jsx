export default function Diamond({ runners = {}, size = 80 }) {
  // runners: { first: bool, second: bool, third: bool }
  const s = size
  const mid = s / 2
  const d = s * 0.3 // diamond radius
  const baseSize = s * 0.08

  const bases = [
    { key: 'second', x: mid, y: mid - d },
    { key: 'third', x: mid - d, y: mid },
    { key: 'first', x: mid + d, y: mid },
  ]

  return (
    <svg width={s} height={s} viewBox={`0 0 ${s} ${s}`} className="inline-block">
      {/* Diamond outline */}
      <polygon
        points={`${mid},${mid - d} ${mid + d},${mid} ${mid},${mid + d} ${mid - d},${mid}`}
        fill="none"
        stroke="#475569"
        strokeWidth="1.5"
      />
      {/* Home plate */}
      <rect
        x={mid - baseSize / 2}
        y={mid + d - baseSize / 2}
        width={baseSize}
        height={baseSize}
        fill="#475569"
        transform={`rotate(45 ${mid} ${mid + d})`}
      />
      {/* Bases */}
      {bases.map(({ key, x, y }) => (
        <rect
          key={key}
          x={x - baseSize / 2}
          y={y - baseSize / 2}
          width={baseSize}
          height={baseSize}
          fill={runners[key] ? '#facc15' : '#334155'}
          stroke="#475569"
          strokeWidth="1"
          transform={`rotate(45 ${x} ${y})`}
        />
      ))}
    </svg>
  )
}
