import { useEffect, useImperativeHandle, forwardRef, useRef } from 'react'

const CountdownBar = forwardRef(function CountdownBar({ seconds = 10, onComplete }, ref) {
  const dotRef = useRef(null)
  const onCompleteRef = useRef(onComplete)
  onCompleteRef.current = onComplete

  useEffect(() => {
    const dot = dotRef.current
    if (!dot) return
    const handler = () => onCompleteRef.current?.()
    dot.addEventListener('animationiteration', handler)
    return () => dot.removeEventListener('animationiteration', handler)
  }, [])

  useImperativeHandle(ref, () => ({
    reset() {
      const dot = dotRef.current
      if (!dot) return

      // Get current position before stopping animation
      const rect = dot.getBoundingClientRect()
      const parentRect = dot.parentElement.getBoundingClientRect()
      const currentLeft = rect.left - parentRect.left

      // Stop the bounce animation, pin at current position
      dot.style.animation = 'none'
      dot.style.left = `${currentLeft}px`
      dot.style.transition = 'none'
      void dot.offsetWidth // force reflow

      // Glide back to start
      dot.style.transition = 'left 0.4s ease-in-out'
      dot.style.left = '0px'

      // After glide completes, restart the bounce
      const onEnd = () => {
        dot.removeEventListener('transitionend', onEnd)
        dot.style.transition = ''
        dot.style.left = ''
        dot.style.animation = ''
      }
      dot.addEventListener('transitionend', onEnd)
    }
  }), [])

  return (
    <div className="countdown-track">
      <div ref={dotRef} className="countdown-dot" style={{ '--cd-duration': `${seconds}s` }} />
      <style>{`
        .countdown-track {
          position: relative;
          height: 6px;
          width: 100%;
        }
        .countdown-track::before {
          content: '';
          position: absolute;
          top: 50%;
          left: 0;
          right: 0;
          height: 1px;
          background: rgba(100,116,139,0.3);
        }
        .countdown-dot {
          position: absolute;
          top: 50%;
          transform: translateY(-50%);
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: rgba(52,211,153,0.7);
          animation: cdBounce var(--cd-duration, 10s) linear infinite alternate;
          will-change: left;
        }
        @keyframes cdBounce {
          from { left: 0; }
          to { left: calc(100% - 6px); }
        }
      `}</style>
    </div>
  )
})

export default CountdownBar
