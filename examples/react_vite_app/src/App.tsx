import { Link, Outlet } from "react-router-dom";

export function App() {
  return (
    <div style={{ fontFamily: "system-ui, sans-serif", margin: "2rem" }}>
      <nav style={{ display: "flex", gap: "1rem", marginBottom: "2rem" }}>
        <Link to="/">Home</Link>
        <Link to="/about">About</Link>
        <Link to="/settings">Settings</Link>
      </nav>
      <Outlet />
    </div>
  );
}
