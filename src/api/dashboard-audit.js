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
  const time = v => v ? new Date(v).toLocaleString('tr-TR') : 'Kayıt bulunamadı';
  const picks = (title, items, timestamp, model) => {
    const box = el('section', null, 'audit-picks');
    box.append(el('h3', title), el('p', 'Zaman: ' + time(timestamp)), el('p', 'Model sürümü: ' + (model || 'Kayıt bulunamadı')));
    if (!items.length) box.append(el('p', 'Bu bölümde doğrulanabilir seçim yok.'));
    items.forEach(p => box.append(el('p', name(p) + (p.probability == null ? '' : ' · Model: %' + (Number(p.probability) * 100).toFixed(1)))));
    return box;
  };
  let seq = 0;
  async function examine(id) {
    const request = ++seq;
    target.replaceChildren(el('p', 'Maç kayıtları okunuyor…'));
    try {
      const response = await fetch('/api/matches/' + id + '/prediction-audit', { headers: {Accept: 'application/json'} });
      if (!response.ok) throw new Error(response.status === 404 ? 'Maç bulunamadı.' : 'API isteği başarısız: ' + response.status);
      const data = await response.json();
      if (request !== seq) return;
      const current = Array.isArray(data.current_recommendations) ? data.current_recommendations : [];
      const saved = Array.isArray(data.original?.picks) ? data.original.picks.filter(p => p && typeof p === 'object') : [];
      const difference = data.comparison?.status === 'different';
      const block = document.createDocumentFragment();
      block.append(el('h2', 'Maç #' + data.match_id));
      block.append(el('p', 'Maç #' + id + ' · İlk kayıtlar değiştirilmez.'));
      block.append(el('p', difference ? 'Güncel ve ilk kayıt seçimleri farklı.' :
        data.comparison?.status === 'same' ? 'Görüntülenen seçimler eşleşiyor.' :
        'Eksik yayın veya kayıt nedeniyle seçimler karşılaştırılamıyor.'));
      const columns = el('div', null, 'audit-columns');
      columns.append(picks('Güncel ana sayfa yayını', current, data.current_generated_at, data.current_model_version));
      columns.append(picks('İlk kaydedilen maç öncesi tahminler', saved, data.original?.published_at, data.original?.model_version));
      block.append(columns);
      block.append(el('p', data.current_note || 'Güncel yayın notu bulunamadı.'));
      const labels = {missing_snapshot:'İlk tahmin kaydı yok', empty_snapshot:'İlk kayıt boş',
        invalid_picks:'Seçim listesi hatalı', invalid_selection:'Seçim kimliği eksik veya hatalı',
        duplicate_selection:'Tekrarlanan seçim', invalid_probability:'Olasılık eksik veya geçersiz',
        missing_model_version:'Model sürümü eksik', league_mismatch:'Lig kaydı uyuşmuyor',
        missing_kickoff:'Başlama zamanı eksik', not_prematch:'Kayıt maç öncesine ait değil',
        future_snapshot:'Kayıt zamanı gelecekte', publication_history_unavailable:'Yayın geçmişi henüz yok',
        current_publication_unavailable:'Geçerli güncel yayın yok'};
      const showChanges = changes => {
        const section = el('div');
        for (const p of changes?.added || []) section.append(el('p', 'Eklenen: ' + name(p)));
        for (const p of changes?.removed || []) section.append(el('p', 'Çıkarılan: ' + name(p)));
        for (const p of changes?.changed || []) section.append(el('p', 'Güncellenen: ' + name(p.after) +
          ' · Olasılık: ' + p.before.probability + ' → ' + p.after.probability));
        return section;
      };
      block.append(showChanges(data.comparison));
      for (const issue of data.issues || []) block.append(el('p', 'Kayıt uyarısı: ' + (labels[issue] || issue)));
      block.append(el('h3', 'Yayın değişiklik geçmişi'), el('p', data.history_note));
      const history = el('div'); block.append(history);
      const appendHistory = rows => {
        for (const row of rows || []) {
          const entry = picks('Yayın #' + row.id, row.picks || [], row.published_at, row.model_version);
          entry.append(el('p', 'Yayın sürümü: ' + row.board_version), showChanges(row.changes));
          if (!row.picks?.length) entry.append(el('p', 'Seçimler yayından kaldırıldı.'));
          history.append(entry);
        }
      };
      appendHistory(data.history);
      let cursor = data.next_before;
      const more = el('button', 'Daha eski yayınlar'); more.type = 'button'; more.hidden = !cursor;
      more.addEventListener('click', async () => {
        more.disabled = true;
        try {
          const result = await fetch('/api/matches/' + id + '/prediction-audit?before=' + cursor);
          if (!result.ok) throw new Error('Geçmiş okunamadı; yeniden deneyin.');
          const next = await result.json(); if (request !== seq) return;
          appendHistory(next.history); cursor = next.next_before; more.hidden = !cursor;
        } catch (err) { more.textContent = err.message; }
        finally { more.disabled = false; }
      });
      block.append(more);
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
