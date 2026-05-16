import { useState } from "react";

export function Home() {
  const [msg, setMsg] = useState("");
  return (
    <main>
      <h1>Home</h1>
      <button
        data-testid="home-action"
        onClick={() => setMsg("Hello from Home!")}
      >
        Greet
      </button>
      <p>{msg}</p>
    </main>
  );
}
