import React, { useEffect, useState } from "react";

export default function LiveBetML() {
  const [events, setEvents] = useState<Array<{minute: number; event: string; team: string; player?: string}>>([]);
  const [ws, setWs] = useState<WebSocket | null>(null);

  useEffect(() => {
    const url = "ws://localhost:8000/ws/live";
    const ws = new WebSocket(url);
    setWs(ws);

    ws.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      setEvents((prev) => [...prev, { minute: ev.minute, event: ev.event, team: ev.team, player: ev.player }]);
    };

    return () => ws.close();
  }, []);

  return (
    <div className="p-6 max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-4">LiveBetML – Canlı Olayları</h1>
      <ul className="space-y-2">
        {events.map((e, i) => (
          <li key={i} className="p-3 rounded bg-gray-100">
            <span className="font-medium">{e.minute}'</span> – {e.event} ({e.team})
            {e.player && <span className="ml-2">({e.player})</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}