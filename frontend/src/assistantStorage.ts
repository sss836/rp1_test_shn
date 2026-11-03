/** Assistant browser data belongs to one verified identity, not the anonymous UI. */
export function clearAccountDrafts(account: string) {
  try {
    const encoded = encodeURIComponent(account);
    for (const key of Object.keys(sessionStorage)) if (key.startsWith(`rp1-assistant-draft:${encoded}:`)) sessionStorage.removeItem(key);
    for (const field of ["thread", "job", "queued"]) sessionStorage.removeItem(`rp1-assistant-${field}:${encoded}`);
  } catch { /* Storage may be unavailable in private browsing. */ }
}
export function clearAssistantStorage() {
  try { for (const key of Object.keys(sessionStorage)) if (key.startsWith("rp1-assistant-")) sessionStorage.removeItem(key); } catch { /* Authentication must still complete if storage is unavailable. */ }
}
