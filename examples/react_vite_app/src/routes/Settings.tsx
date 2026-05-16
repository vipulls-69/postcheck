import { useState } from "react";

export function Settings() {
  const [saved, setSaved] = useState(false);
  return (
    <main>
      <h1>Settings</h1>
      <button data-testid="settings-action" onClick={() => setSaved(true)}>
        Save
      </button>
      <p>{saved ? "Settings saved." : "Unsaved changes."}</p>
    </main>
  );
}
