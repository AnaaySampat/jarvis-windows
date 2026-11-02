import { useEffect, useState } from "react";
import { Card, Row, Note } from "./controls";

export default function TaskPasskeySettings({ state, onLoad, onSave }) {
  const [passkey, setPasskey] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [savingFrom, setSavingFrom] = useState(undefined);
  const saving = savingFrom !== undefined && savingFrom === state;
  useEffect(() => { onLoad?.(); }, [onLoad]);
  const save = (event) => {
    event.preventDefault();
    if (passkey !== confirm) { setError("The passkeys do not match."); return; }
    if (passkey.length < 8 || passkey.length > 128) {
      setError("Use between 8 and 128 characters."); return;
    }
    if (!onSave?.(passkey)) { setError("The laptop backend is disconnected. Try again when it reconnects."); return; }
    setError("");
    setPasskey("");
    setConfirm("");
    setSavingFrom(state);
  };
  return (
    <Card title="Phone-task passkey" sub="Enter this passkey on your phone for every new PC task. Laptop tasks do not require it.">
      <form className="sx-card-body" onSubmit={save}>
        <Note tone={state?.configured ? "ok" : "warn"}>
          {state?.configured ? "A passkey is set. Save a new one below to change it."
            : "Set a passkey here before sending tasks from your phone."}
        </Note>
        <Row title={state?.configured ? "New passkey" : "Passkey"} desc="8–128 characters, separate from the one-time pairing PIN." stack>
          <input type="password" autoComplete="off" aria-label="Phone-task passkey"
            value={passkey} minLength={8} maxLength={128} required
            onChange={(event) => setPasskey(event.target.value)} />
        </Row>
        <Row title="Confirm passkey" stack>
          <input type="password" autoComplete="off" aria-label="Confirm phone-task passkey"
            value={confirm} minLength={8} maxLength={128} required
            onChange={(event) => setConfirm(event.target.value)} />
        </Row>
        {(error || state?.error) && <Note tone="err">{error || state.error}</Note>}
        {!saving && state?.saved && !error && <Note tone="ok">Passkey saved on this laptop.</Note>}
        <button type="submit" className="btn btn--primary" disabled={saving || !passkey || !confirm}>
          {saving ? "Saving…" : state?.configured ? "Change passkey" : "Set passkey"}
        </button>
      </form>
    </Card>
  );
}
