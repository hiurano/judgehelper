'use strict';

// Each account generation has its own database. Web Locks keep a recording
// claimed until its tab closes, or explicitly releases it after completion.
class RecordingStore {
    constructor(owner, db) {
        this.owner = owner;
        this.db = db;
        this.claims = new Map();
    }

    static async open(owner) {
        if (!owner || !navigator.locks) throw new Error('Браузер не поддерживает безопасное сохранение записи.');
        const db = await new Promise((resolve, reject) => {
            const request = indexedDB.open(`JudgeHelperRecordings-${owner}`, 1);
            request.onupgradeneeded = () => {
                const db = request.result;
                db.createObjectStore('recordings', { keyPath: 'id' });
                const chunks = db.createObjectStore('chunks', { keyPath: ['recordingId', 'sequence'] });
                chunks.createIndex('recordingId', 'recordingId');
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
        return new RecordingStore(owner, db);
    }

    transaction(mode, callback) {
        return new Promise((resolve, reject) => {
            const tx = this.db.transaction(['recordings', 'chunks'], mode);
            let result;
            tx.oncomplete = () => resolve(result);
            tx.onerror = tx.onabort = () => reject(tx.error || new Error('Не удалось сохранить запись'));
            try {
                callback(tx.objectStore('recordings'), tx.objectStore('chunks'), value => { result = value; });
            } catch (error) {
                tx.abort();
                reject(error);
            }
        });
    }

    async claim(id) {
        if (this.claims.has(id)) return false;
        return new Promise((resolve, reject) => {
            navigator.locks.request(`judge-recording:${this.owner}:${id}`, { ifAvailable: true }, async lock => {
                if (!lock) { resolve(false); return; }
                await new Promise(release => {
                    this.claims.set(id, release);
                    resolve(true);
                });
            }).catch(reject);
        });
    }

    release(id) {
        const release = this.claims.get(id);
        this.claims.delete(id);
        if (release) release();
    }

    async create(filename) {
        const record = { id: crypto.randomUUID(), filename, createdAt: Date.now(), jobId: null };
        await this.claim(record.id);
        try {
            await this.transaction('readwrite', recordings => recordings.add(record));
            return record;
        } catch (error) {
            this.release(record.id);
            throw error;
        }
    }

    append(id, sequence, blob) {
        return this.transaction('readwrite', (_recordings, chunks) => {
            chunks.add({ recordingId: id, sequence, blob });
        });
    }

    attachJob(id, jobId) {
        return this.transaction('readwrite', recordings => {
            const request = recordings.get(id);
            request.onsuccess = () => {
                if (request.result) recordings.put({ ...request.result, jobId });
            };
        });
    }

    async remove(id) {
        await this.transaction('readwrite', (recordings, chunks) => {
            recordings.delete(id);
            const request = chunks.index('recordingId').openKeyCursor(IDBKeyRange.only(id));
            request.onsuccess = () => {
                const cursor = request.result;
                if (cursor) { chunks.delete(cursor.primaryKey); cursor.continue(); }
            };
        });
        this.release(id);
    }

    async recover() {
        const records = await this.transaction('readonly', (recordings, _chunks, result) => {
            const request = recordings.getAll();
            request.onsuccess = () => result(request.result);
        });
        const recovered = [];
        for (const record of records) {
            if (!await this.claim(record.id)) continue;
            try {
                const parts = await this.transaction('readonly', (_recordings, chunks, result) => {
                    const request = chunks.index('recordingId').getAll(IDBKeyRange.only(record.id));
                    request.onsuccess = () => result(request.result);
                });
                if (!parts.length) { await this.remove(record.id); continue; }
                parts.sort((a, b) => a.sequence - b.sequence);
                const type = parts[0].blob.type || 'audio/webm';
                const filename = `${record.filename}.${type.includes('mp4') ? 'mp4' : 'webm'}`;
                recovered.push({ ...record, file: new File(parts.map(part => part.blob), filename, { type }) });
            } catch (error) {
                this.release(record.id);
                throw error;
            }
        }
        return recovered;
    }
}

window.RecordingStore = RecordingStore;
