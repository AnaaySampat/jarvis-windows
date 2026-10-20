// controls.jsx — the small building blocks the menus share (styles in ui.css):
// a settings Card with Rows, a Switch, a collapsible Guide, a Note, a two-click
// ConfirmButton for anything destructive, and a removable ChipList + AddField pair.

import { useState } from "react";
import { Icon } from "../hud/icons";

export function Card({ title, sub, status, actions, children, className = "" }) {
  return (
    <section className={`sx-card ${className}`}>
      {(title || actions) && (
        <header className="sx-card-hd">
          <div className="sx-card-txt">
            {title && <h3 className="sx-card-title">{title}{status}</h3>}
            {sub && <p className="sx-card-sub">{sub}</p>}
          </div>
          {actions && <div className="sx-card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

/** One setting: title + description on the left, its control on the right. `label`
 *  makes the whole row toggle the switch inside it; `stack` puts the control below. */
export function Row({ title, desc, children, stack, label, extra }) {
  const Tag = label ? "label" : "div";
  return (
    <Tag className={`sx-row${stack ? " sx-row--stack" : ""}`}>
      <div className="sx-row-txt">
        <div className="sx-row-t">{title}</div>
        {desc && <div className="sx-row-d">{desc}</div>}
      </div>
      {children != null && children !== false && <div className="sx-row-c">{children}</div>}
      {extra && <div className="sx-row-extra">{extra}</div>}
    </Tag>
  );
}

export function Switch({ checked, onChange, label, disabled }) {
  return (
    <input type="checkbox" role="switch" className="sw" checked={!!checked} disabled={disabled}
           aria-label={label} onChange={(e) => onChange(e.target.checked)} />
  );
}

export function Guide({ summary = "How to set it up", children }) {
  return (
    <details className="sx-guide">
      <summary><Icon name="chevRight" size={14} />{summary}</summary>
      <div className="sx-guide-body">{children}</div>
    </details>
  );
}

const NOTE_ICON = { info: "info", warn: "warning", err: "warning", ok: "check" };
export function Note({ tone = "info", children }) {
  return (
    <div className={`note note--${tone}`} role={tone === "err" ? "alert" : undefined}>
      <Icon name={NOTE_ICON[tone] || "info"} size={15} />
      <div>{children}</div>
    </div>
  );
}

/** The first click arms, the second acts; moving focus away disarms. */
export function ConfirmButton({ children, confirmLabel = "Confirm", onConfirm, title,
  className = "btn btn--danger btn--sm" }) {
  const [armed, setArmed] = useState(false);
  return (
    <button type="button" className={`${className}${armed ? " is-armed" : ""}`} title={title}
            onClick={(e) => {
              e.preventDefault();          // inside a <label>/<summary>: don't toggle it
              if (armed) { setArmed(false); onConfirm(); } else setArmed(true);
            }}
            onBlur={() => setArmed(false)}>
      {armed ? confirmLabel : children}
    </button>
  );
}

export function ChipList({ items, onRemove, empty, removeLabel = "Remove" }) {
  if (!items.length) return <div className="chips-empty">{empty}</div>;
  return (
    <div className="chips">
      {items.map((it) => (
        <span key={it} className="chip" title={it}>
          <span>{it}</span>
          <button type="button" aria-label={`${removeLabel} ${it}`} title={removeLabel}
                  onClick={() => onRemove(it)}>
            <Icon name="close" size={12} stroke={2} />
          </button>
        </span>
      ))}
    </div>
  );
}

/** A text field with an action button; Enter submits. Clears itself after onAdd. */
export function AddField({ placeholder, button = "Add", onAdd, mono = true, label }) {
  const [text, setText] = useState("");
  const submit = (e) => {
    e.preventDefault();
    const v = text.trim();
    if (!v) return;
    onAdd(v);
    setText("");
  };
  return (
    <form className="field-row" onSubmit={submit}>
      <input type="text" className={`field${mono ? " field--mono" : ""}`} value={text}
             placeholder={placeholder} aria-label={label || placeholder}
             onChange={(e) => setText(e.target.value)} spellCheck={false} />
      <button type="submit" className="btn" disabled={!text.trim()}>{button}</button>
    </form>
  );
}
