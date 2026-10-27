/* Read-only comparison of the current league publication and immutable prematch snapshot. */
(() => {
  'use strict';
  const form = document.getElementById('audit-form');
  const input = document.getElementById('audit-match-id');
  const target = document.getElementById('audit-result');
  if (!form || !input || !target) return;
  const el = (tag, value, className) => {
    const node = document.createElement(tag);
    if (value != null) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  };
  const name = p => p.label || p.selection || p.id || 'Seçim adı bilinmiyor';
  const identity = p => [p.market || '', p.selection || '', p.id || ''].join('|');
  const time = v => v ? new Date(v).toLocaleString('tr-TR') : 'Kayıt bulunamadı';
  const picks = (title, items, timestamp) => {
    const box = el('section', null, 'audit-picks');
    box.append(el('h3', title), el('p', 'Zaman: ' + time(timestamp)));
    if (!items.length) box.append(el('p', 'Bu bölümde doğrulanabilir seçim yok.'));
    items.forEach(p => box.append(el('p', name(p) + (p.probability == null ? '' : ' · Model: %' + (Number(p.probability) * 100).toFixed(1)))));
    return box;
  };
  let seq = 0;
  async function examine(id) {
    const request = ++seq;
    target.replaceChildren(el('p', 'Maç kayıtları okunuyor…'));
    try {
      const response = await fetch('/api/matches/' + id + '/dossier', { headers: {Accept: 'application/json'} });
      if (!response.ok) throw new Error(response.status === 404 ? 'Maç bulunamadı.' : 'API isteği başarısız: ' + response.status);
      const data = await response.json();
      if (request !== seq) return;
      const current = Array.isArray(data.current_recommendations) ? data.current_recommendations : [];
      const saved = Array.isArray(data.predictions) ? data.predictions.map(v => v.pick).filter(Boolean) : [];
      const live = new Set(current.map(identity));
      const original = new Set(saved.map(identity));
      const difference = current.length && saved.length &&
        (current.some(p => !original.has(identity(p))) || saved.some(p => !live.has(identity(p))));
      const block = document.createDocumentFragment();
      block.append(el('h2', (data.match?.home_team || 'Ev sahibi') + ' – ' + (data.match?.away_team || 'Deplasman')));
      block.append(el('p', 'Maç #' + id + ' · İlk kayıtlar değiştirilmez.'));
      block.append(el('p', difference ? 'Güncel ve ilk kayıt seçimleri farklı.' :
        current.length && saved.length ? 'Görüntülenen seçimler eşleşiyor.' :
        'Eksik yayın veya kayıt nedeniyle seçimler karşılaştırılamıyor.'));
      const columns = el('div', null, 'audit-columns');
      columns.append(picks('Güncel ana sayfa yayını', current, data.current_generated_at));
      columns.append(picks('İlk kaydedilen maç öncesi tahminler', saved, data.captured_at));
      block.append(columns);
      block.append(el('p', data.current_note || 'Güncel yayın notu bulunamadı.'));
      const a = el('a', 'Maç detayına ve sonuçlarına git'); a.href = '/match/' + id + '#predictions';
      block.append(a);
      target.replaceChildren(block);
    } catch (err) {
      if (request === seq) target.replaceChildren(el('p', err.message || 'Veri okunamadı.'));
    }
  }
  form.addEventListener('submit', e => {
    e.preventDefault();
    const id = Number(input.value);
    if (!Number.isSafeInteger(id) || id < 1) {
      target.replaceChildren(el('p', 'Geçerli bir maç numarası girin.'));
      return;
    }
    examine(id);
  });
})();
