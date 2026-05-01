export default function App() {
  const target = "/app?mode=eval";

  return (
    <main className="moved-shell">
      <section className="moved-panel">
        <p className="eyebrow">Strategy Evaluation UI</p>
        <h1>Moved to Quiet Operator</h1>
        <p>The evaluation console now lives in the unified dashboard.</p>
        <a href={target}>Open /app?mode=eval</a>
      </section>
    </main>
  );
}
