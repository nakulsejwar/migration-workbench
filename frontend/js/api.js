// Every authoritative operation goes through here. No business logic and no data storage live in the browser.
const USER_KEY = 'wb:ui:user'; // a non-critical UI preference (which demo identity to send)

export class ApiError extends Error {
  constructor(status, code, message, details, requestId) { super(message); this.status = status; this.code = code; this.details = details; this.requestId = requestId; }
}
export const getUser = () => { try { return localStorage.getItem(USER_KEY) || ''; } catch (e) { return ''; } };
export const setUser = u => { try { localStorage.setItem(USER_KEY, u); } catch (e) { /* preference only */ } };

export async function api(method, path, body) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') { headers['Content-Type'] = 'application/json'; const u = getUser(); if (u) headers['X-Demo-User'] = u; }
  let res;
  try { res = await fetch('/api' + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }); }
  catch (e) { throw new ApiError(0, 'network', 'Cannot reach the server. Check your connection and try again.', null, null); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { /* non-JSON body */ }
  if (!res.ok) {
    const er = data && data.error;
    throw new ApiError(res.status, er ? er.code : 'http_' + res.status, er ? er.message : `Server error (${res.status})`, er ? er.details : null, er ? er.request_id : res.headers.get('X-Request-ID'));
  }
  return data;
}
export const get = p => api('GET', p);
export const post = (p, b) => api('POST', p, b === undefined ? {} : b);
export const put = (p, b) => api('PUT', p, b);
