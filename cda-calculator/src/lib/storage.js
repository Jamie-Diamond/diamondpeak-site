import { openDB } from 'idb';

const DB_NAME = 'cda-cache';
const STORE_NAME = 'rides';
const MAX_RIDES = 3;

// ── localStorage helpers ──

const LS_KEY = 'cda-sidebar-inputs';

export function loadSidebarInputs() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function saveSidebarInputs(inputs) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(inputs));
  } catch {
    // quota exceeded — ignore
  }
}

// ── IndexedDB helpers ──

function getDB() {
  return openDB(DB_NAME, 1, {
    upgrade(db) {
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'key' });
      }
    },
  });
}

export function rideKey(filename, fileSize) {
  return `${filename}::${fileSize}`;
}

export async function getCachedRide(key) {
  const db = await getDB();
  return db.get(STORE_NAME, key);
}

export async function cacheRide(key, data) {
  const db = await getDB();
  // Evict oldest if at limit
  const all = await db.getAll(STORE_NAME);
  if (all.length >= MAX_RIDES) {
    all.sort((a, b) => a.parsedAt - b.parsedAt);
    const toEvict = all.length - MAX_RIDES + 1;
    const tx = db.transaction(STORE_NAME, 'readwrite');
    for (let i = 0; i < toEvict; i++) {
      tx.store.delete(all[i].key);
    }
    await tx.done;
  }
  await db.put(STORE_NAME, { key, parsedAt: Date.now(), ...data });
}

export async function clearCachedRides() {
  const db = await getDB();
  await db.clear(STORE_NAME);
}
