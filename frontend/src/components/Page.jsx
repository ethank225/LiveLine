// Wraps a page with a subtle fade-in transition on mount.
export default function Page({ className = '', children }) {
  return (
    <div className={`page-enter ${className}`}>
      {children}
    </div>
  )
}
