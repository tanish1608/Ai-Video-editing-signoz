/**
 * Backend URL resolver.
 * 
 * In the Electron app, the backend URL comes from the main process.
 * We cache it after first fetch and fall back to localhost:8080.
 */

let _backendUrl: string | null = null;

export async function getBackendUrl(): Promise<string> {
  if (_backendUrl) return _backendUrl;

  if (window.electron) {
    try {
      _backendUrl = await window.electron.getBackendUrl();
      return _backendUrl;
    } catch {
      // Fallback
    }
  }

  _backendUrl = "http://localhost:8080";
  return _backendUrl;
}

/**
 * Get the WebSocket URL for the backend.
 */
export function getWebSocketUrl(backendUrl: string): string {
  return backendUrl.replace(/^http/, "ws") + "/ws";
}

/**
 * Synchronous backend URL — use after initial async resolution.
 * Falls back to localhost:8080 if not yet resolved.
 */
export function getBackendUrlSync(): string {
  return _backendUrl ?? "http://localhost:8080";
}

/**
 * Resolve a potentially-relative backend URL to an absolute URL.
 * If the url is already absolute (http/https/blob), returns as-is.
 * If relative (starts with "/"), prepends the backend base URL.
 */
export function resolveBackendUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  if (url.startsWith("http://") || url.startsWith("https://") || url.startsWith("blob:")) {
    return url;
  }
  if (url.startsWith("/")) {
    return `${getBackendUrlSync()}${url}`;
  }
  return url;
}
