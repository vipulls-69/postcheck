import { useState } from "react";

export function About() {
  const [msg, setMsg] = useState("");
  return (
    <main>
      <h1>About</h1>
      <button
        data-testid="about-action"
        onClick={() => setMsg("This is the About page.")}
      >
        Show info
      </button>
      <p>{msg}</p>
    </main>
  );
}
