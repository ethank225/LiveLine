import React from "react";

export default function SearchBar({ value, onChange, count, total }) {
  return (
    <>
      <div className="search-wrap">
        <span className="search-icon">⌕</span>
        <input
          className="search-input"
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Search market, side, game…"
        />
      </div>
      {value && <div className="search-count">{count} of {total}</div>}
    </>
  );
}
