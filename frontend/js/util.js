export const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const num = n => Number(n).toLocaleString('en-US');
export const when = t => { try { return new Date(t).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' }); } catch (e) { return t || ''; } };
export const money = m => (m < 0 ? '-' : '') + (Math.abs(m) / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const chip = (t, k) => `<span class="chip ${k || ''}">${esc(t)}</span>`;
export const spin = t => `<span class="spin"></span>${esc(t)}`;
