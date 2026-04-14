import React from "react";

const OPTIONS = [
  { id: "none", label: "All trades" },
  { id: "game", label: "Group by game" },
];

export default function GroupToggle({ value, onChange }) {
  return (
    <div className="toggle-group">
      {OPTIONS.map((t) => (
        <button
          key={t.id}
          className={`toggle-btn ${value === t.id ? "active" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
