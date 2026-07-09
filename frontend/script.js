// API base URL
const API_URL = ''; // Relative to host

// Canvas context
const canvas = document.getElementById('facadeCanvas');
const ctx = canvas.getContext('2d');

const PHYSICAL_WIDTH = 48;
const PHYSICAL_HEIGHT = 96;
const PIXEL_SIZE = 8; // Canvas size: 384x768 (48 * 8 = 384, 96 * 8 = 768)

// Local audio synchronization
const localAudio = document.getElementById('localAudio');
let currentAudioUrl = null;

// UI Elements
const statusIndicator = document.getElementById('statusIndicator');
const statusText = document.getElementById('statusText');
const currentTrackTitle = document.getElementById('currentTrackTitle');
const currentTrackStatus = document.getElementById('currentTrackStatus');
const timeElapsed = document.getElementById('timeElapsed');
const timeTotal = document.getElementById('timeTotal');
const progressBarFill = document.getElementById('progressBarFill');
const progressBarBg = document.getElementById('progressBarBg');
const togglePlayBtn = document.getElementById('togglePlayBtn');
const skipBtn = document.getElementById('skipBtn');
const audioBadge = document.getElementById('audioBadge');
const volumeControlContainer = document.getElementById('volumeControlContainer');
const muteBtn = document.getElementById('muteBtn');
const volumeSlider = document.getElementById('volumeSlider');
const uploadZone = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');
const uploadProgressContainer = document.getElementById('uploadProgressContainer');
const uploadProgressFill = document.getElementById('uploadProgressFill');
const uploadProgressStatus = document.getElementById('uploadProgressStatus');
const queueList = document.getElementById('queueList');
const libraryList = document.getElementById('libraryList');
const queueCount = document.getElementById('queueCount');
const libraryCount = document.getElementById('libraryCount');

// Layout Cache (will be dynamically detected from the grid)
let isWindowMap = new Uint8Array(PHYSICAL_WIDTH * PHYSICAL_HEIGHT);
let windowMapInitialized = false;

// -------------------------------------------------------------
// HELPER FUNCTIONS
// -------------------------------------------------------------
function formatTime(ms) {
    if (isNaN(ms) || ms < 0) return '00:00';
    const totalSecs = Math.floor(ms / 1000);
    const mins = Math.floor(totalSecs / 60);
    const secs = totalSecs % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

// Check if a pixel is structurally a window on the Schonherz facade
function initWindowMap() {
    for (let y = 0; y < PHYSICAL_HEIGHT; y++) {
        for (let x = 0; x < PHYSICAL_WIDTH; x++) {
            let isWin = false;
            
            // Left Half windows (w_col: 0..7)
            if (x >= 0 && x <= 22) {
                if (x % 3 !== 2) isWin = true;
            }
            // Right Half windows (w_col: 8..15)
            else if (x >= 25 && x <= 47) {
                if ((x - 25) % 3 !== 2) isWin = true;
            }
            
            // Floor checks
            let onActiveFloor = false;
            
            // Floors 6 to 18 (P pixels, rows 4 to 53)
            if (y >= 3 && y <= 52) {
                let floorOffset = y - 3;
                if (floorOffset % 4 === 0 || floorOffset % 4 === 1) {
                    onActiveFloor = true;
                }
            }
            // Floors -1 to 5 (X pixels, rows 55 to 80)
            else if (y >= 55 && y <= 80) {
                // F5: rows 55, 56 (0-indexed 55, 56 corresponds to row 56, 57 1-indexed)
                // F4: rows 59, 60
                // F3: rows 63, 64
                // F2: rows 67, 68
                // F1: row 71
                // F0: row 75
                // F-1: rows 78, 79, 80
                if (y === 55 || y === 56 || 
                    y === 59 || y === 60 || 
                    y === 63 || y === 64 || 
                    y === 67 || y === 68 || 
                    y === 71 || 
                    y === 75 || 
                    y === 78 || y === 79 || y === 80) {
                    onActiveFloor = true;
                }
            }
            
            isWindowMap[y * PHYSICAL_WIDTH + x] = (isWin && onActiveFloor) ? 1 : 0;
        }
    }
    windowMapInitialized = true;
}

// -------------------------------------------------------------
// RENDER LOOP
// -------------------------------------------------------------
function renderFacade(pixelBytes) {
    if (!windowMapInitialized) {
        initWindowMap();
    }
    
    // Clear canvas
    ctx.fillStyle = '#06070a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    
    // Draw building background structure
    ctx.fillStyle = '#101116';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    
    // Draw floors separator indicators (vertical concrete bands)
    ctx.fillStyle = '#16171f';
    ctx.fillRect(23 * PIXEL_SIZE, 0, 2 * PIXEL_SIZE, canvas.height); // Center elevator block
    
    // Render pixels
    for (let y = 0; y < PHYSICAL_HEIGHT; y++) {
        for (let x = 0; x < PHYSICAL_WIDTH; x++) {
            const idx = y * PHYSICAL_WIDTH + x;
            const offset = idx * 3;
            
            const r = pixelBytes[offset];
            const g = pixelBytes[offset + 1];
            const b = pixelBytes[offset + 2];
            const isLit = (r > 0 || g > 0 || b > 0);
            
            const px = x * PIXEL_SIZE;
            const py = y * PIXEL_SIZE;
            
            if (isWindowMap[idx]) {
                // If it is a window
                if (isLit) {
                    // Window is ON (glowing)
                    ctx.shadowBlur = 8;
                    ctx.shadowColor = `rgb(${r}, ${g}, ${b})`;
                    ctx.fillStyle = `rgb(${r}, ${g}, ${b})`;
                    ctx.fillRect(px + 1, py + 1, PIXEL_SIZE - 2, PIXEL_SIZE - 2);
                    
                    // Reset shadow
                    ctx.shadowBlur = 0;
                } else {
                    // Window is OFF (dark glass)
                    ctx.fillStyle = '#090a0f';
                    ctx.fillRect(px + 1, py + 1, PIXEL_SIZE - 2, PIXEL_SIZE - 2);
                    // Subtle window border
                    ctx.strokeStyle = 'rgba(255,255,255,0.03)';
                    ctx.strokeRect(px + 1, py + 1, PIXEL_SIZE - 2, PIXEL_SIZE - 2);
                }
            } else {
                // It is a building wall
                ctx.fillStyle = '#1a1b24';
                ctx.fillRect(px, py, PIXEL_SIZE, PIXEL_SIZE);
                
                // Add concrete lines on wall blocks to make it look structural
                if (x % 3 === 2 || y % 4 === 2) {
                    ctx.fillStyle = '#14151b';
                    ctx.fillRect(px, py, PIXEL_SIZE, PIXEL_SIZE);
                }
            }
        }
    }
}

// -------------------------------------------------------------
// POLLING API STATUS
// -------------------------------------------------------------
async function updateFrameAndStatus() {
    try {
        // Fetch current frame binary & playback details
        const res = await fetch('/api/esp/current-frame/json');
        if (!res.ok) throw new Error('API error');
        
        const data = await res.json();
        
        // Decode base64 pixels
        const binaryString = atob(data.pixels);
        const pixelBytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
            pixelBytes[i] = binaryString.charCodeAt(i);
        }
        
        // Render
        renderFacade(pixelBytes);
        
        // Update playback status
        const status = data.status;
        updatePlaybackUI(status);
        
    } catch (e) {
        console.error('Failed to fetch status:', e);
    }
}

function updatePlaybackUI(status) {
    // 1. Status indicator
    statusIndicator.className = 'status-indicator';
    if (status.is_idle) {
        statusIndicator.classList.add('idle');
        statusText.textContent = 'KÉSZENLÉT';
        
        currentTrackTitle.textContent = 'Készenléti szimuláció';
        currentTrackStatus.textContent = 'Ablakfények véletlenszerű villódzása';
        progressBarFill.style.width = '0%';
        timeElapsed.textContent = '00:00';
        timeTotal.textContent = '00:00';
        
        togglePlayBtn.disabled = true;
        skipBtn.disabled = true;
        
        // Stop audio
        if (currentAudioUrl) {
            localAudio.pause();
            currentAudioUrl = null;
            localAudio.src = '';
            audioBadge.style.display = 'none';
            volumeControlContainer.style.display = 'none';
        }
    } else {
        const isPlaying = status.is_playing;
        statusIndicator.classList.add(isPlaying ? 'active' : 'paused');
        statusText.textContent = isPlaying ? 'MŰSORSZÓRÁS' : 'SZÜNET';
        
        currentTrackTitle.textContent = status.name || 'Névtelen animáció';
        currentTrackStatus.textContent = isPlaying ? 'Lejátszás folyamatban...' : 'Megállítva';
        
        togglePlayBtn.disabled = false;
        skipBtn.disabled = false;
        
        // Update play/pause button icon
        if (isPlaying) {
            togglePlayBtn.innerHTML = '<svg viewBox="0 0 24 24" class="btn-icon"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        } else {
            togglePlayBtn.innerHTML = '<svg viewBox="0 0 24 24" class="btn-icon"><path d="M8 5v14l11-7z"/></svg>';
        }
        
        // Duration and progress
        const dbEntry = libraryCache[status.animation_id];
        const durationMs = dbEntry ? dbEntry.duration_ms : 180000; // fallback to 3 mins
        const elapsedMs = status.elapsed_time_ms;
        const progressPercent = Math.min(100, (elapsedMs / durationMs) * 100);
        
        progressBarFill.style.width = `${progressPercent}%`;
        timeElapsed.textContent = formatTime(elapsedMs);
        timeTotal.textContent = formatTime(durationMs);
        
        // Handle sync Audio
        if (status.audio_url) {
            audioBadge.style.display = 'block';
            volumeControlContainer.style.display = 'flex';
            
            if (currentAudioUrl !== status.audio_url) {
                currentAudioUrl = status.audio_url;
                localAudio.src = status.audio_url;
                localAudio.load();
                localAudio.volume = volumeSlider.value;
            }
            
            // Sync play state
            if (isPlaying) {
                if (localAudio.paused) {
                    localAudio.play().catch(e => console.log('Audio playback block:', e));
                }
                
                // Keep sync (if diff is greater than 0.3s)
                const localSec = localAudio.currentTime;
                const serverSec = elapsedMs / 1000.0;
                if (Math.abs(localSec - serverSec) > 0.3) {
                    localAudio.currentTime = serverSec;
                }
            } else {
                if (!localAudio.paused) {
                    localAudio.pause();
                }
            }
        } else {
            // No audio in this animation
            if (currentAudioUrl) {
                localAudio.pause();
                currentAudioUrl = null;
                localAudio.src = '';
            }
            audioBadge.style.display = 'none';
            volumeControlContainer.style.display = 'none';
        }
    }
}

// -------------------------------------------------------------
// LISTS AND QUEUE DATA MANAGEMENT
// -------------------------------------------------------------
let libraryCache = {};

async function fetchLibraryAndQueue() {
    try {
        // Fetch queue status and uploaded animations
        const res = await fetch('/api/playback/status');
        if (!res.ok) throw new Error();
        const data = await res.json();
        
        const resAnim = await fetch('/api/animations');
        if (!resAnim.ok) throw new Error();
        const animations = await resAnim.json();
        
        // Rebuild library cache
        libraryCache = {};
        animations.forEach(a => {
            libraryCache[a.id] = a;
        });
        
        renderLibrary(animations);
        renderQueue(data.queue);
        
    } catch (e) {
        console.error('Failed to fetch lists:', e);
    }
}

function renderLibrary(animations) {
    libraryCount.textContent = `${animations.length} darab`;
    
    if (animations.length === 0) {
        libraryList.innerHTML = '<div class="empty-state">Még nem töltöttek fel animációt.</div>';
        return;
    }
    
    // Sort by uploaded time (newest first)
    animations.sort((a, b) => b.uploaded_at - a.uploaded_at);
    
    libraryList.innerHTML = animations.map((anim, index) => {
        const dateStr = new Date(anim.uploaded_at * 1000).toLocaleString('hu-HU', {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
        });
        const hasAudio = anim.audio_url ? `
            <svg viewBox="0 0 24 24" class="audio-icon-small" title="Hanggal rendelkezik">
                <path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/>
            </svg>
        ` : '';
        
        return `
            <div class="item-row">
                <div class="item-num">${index + 1}</div>
                <div class="item-details">
                    <div class="item-name" title="${anim.name}">${anim.name}</div>
                    <div class="item-meta">
                        <span class="item-duration">${formatTime(anim.duration_ms)}</span>
                        &bull; <span>${dateStr}</span>
                        ${hasAudio}
                    </div>
                </div>
                <div class="item-actions">
                    <button class="action-btn btn-play" onclick="addToQueue('${anim.id}')" title="Hozzáadás a várólistához">
                        <svg viewBox="0 0 24 24" class="action-svg"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
                    </button>
                    <button class="action-btn btn-delete" onclick="deleteAnimation('${anim.id}')" title="Törlés">
                        <svg viewBox="0 0 24 24" class="action-svg"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

function renderQueue(queue) {
    queueCount.textContent = `${queue.length} darab`;
    
    if (queue.length === 0) {
        queueList.innerHTML = '<div class="empty-state">Nincs animáció a várólistában.</div>';
        return;
    }
    
    queueList.innerHTML = queue.map((anim, index) => {
        return `
            <div class="item-row">
                <div class="item-num">${index + 1}</div>
                <div class="item-details">
                    <div class="item-name">${anim.name}</div>
                    <div class="item-meta">
                        <span class="item-duration">${formatTime(anim.duration_ms)}</span>
                    </div>
                </div>
                <div class="item-actions">
                    <button class="action-btn btn-delete" onclick="removeFromQueue('${anim.id}')" title="Eltávolítás a sorból">
                        <svg viewBox="0 0 24 24" class="action-svg"><path d="M19 13H5v-2h14v2z"/></svg>
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

// -------------------------------------------------------------
// CONTROL BUTTONS ACTIONS
// -------------------------------------------------------------
async function addToQueue(id) {
    try {
        const res = await fetch('/api/queue/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ animation_id: id })
        });
        if (res.ok) {
            fetchLibraryAndQueue();
        }
    } catch (e) {
        console.error(e);
    }
}

async function removeFromQueue(id) {
    try {
        const res = await fetch('/api/queue/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ animation_id: id })
        });
        if (res.ok) {
            fetchLibraryAndQueue();
        }
    } catch (e) {
        console.error(e);
    }
}

async function deleteAnimation(id) {
    if (!confirm('Biztosan törölni szeretnéd ezt az animációt?')) return;
    try {
        const res = await fetch(`/api/animations/${id}`, {
            method: 'DELETE'
        });
        if (res.ok) {
            fetchLibraryAndQueue();
        }
    } catch (e) {
        console.error(e);
    }
}

// Playback control event listeners
togglePlayBtn.addEventListener('click', async () => {
    try {
        const res = await fetch('/api/playback/toggle', { method: 'POST' });
        if (res.ok) {
            updateFrameAndStatus();
        }
    } catch (e) {
        console.error(e);
    }
});

skipBtn.addEventListener('click', async () => {
    try {
        const res = await fetch('/api/playback/skip', { method: 'POST' });
        if (res.ok) {
            updateFrameAndStatus();
        }
    } catch (e) {
        console.error(e);
    }
});

// -------------------------------------------------------------
// AUDIO VOLUME CONTROLS
// -------------------------------------------------------------
let isMuted = false;
let previousVolume = 0.5;

muteBtn.addEventListener('click', () => {
    isMuted = !isMuted;
    localAudio.muted = isMuted;
    
    if (isMuted) {
        muteBtn.innerHTML = '<svg viewBox="0 0 24 24" class="btn-icon"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.21.05-.42.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.03c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73 4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>';
        volumeSlider.value = 0;
    } else {
        muteBtn.innerHTML = '<svg viewBox="0 0 24 24" class="btn-icon"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>';
        volumeSlider.value = previousVolume;
        localAudio.volume = previousVolume;
    }
});

volumeSlider.addEventListener('input', (e) => {
    const val = parseFloat(e.target.value);
    localAudio.volume = val;
    if (val > 0) {
        isMuted = false;
        localAudio.muted = false;
        previousVolume = val;
        muteBtn.innerHTML = '<svg viewBox="0 0 24 24" class="btn-icon"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>';
    } else {
        isMuted = true;
        localAudio.muted = true;
        muteBtn.innerHTML = '<svg viewBox="0 0 24 24" class="btn-icon"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.21.05-.42.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.03c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73 4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>';
    }
});

// -------------------------------------------------------------
// DRAG AND DROP UPLOAD
// -------------------------------------------------------------
uploadZone.addEventListener('click', () => fileInput.click());

uploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadZone.classList.add('dragover');
});

uploadZone.addEventListener('dragleave', () => {
    uploadZone.classList.remove('dragover');
});

uploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadZone.classList.remove('dragover');
    
    if (e.dataTransfer.files.length > 0) {
        handleUpload(e.dataTransfer.files[0]);
    }
});

fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
        handleUpload(e.target.files[0]);
    }
});

async function handleUpload(file) {
    if (!file.name.endsWith('.q4x')) {
        alert('Csak .q4x fájlokat tölthetsz fel!');
        return;
    }
    
    const formData = new FormData();
    formData.append('file', file);
    
    uploadProgressContainer.style.display = 'block';
    uploadProgressFill.style.width = '0%';
    uploadProgressStatus.textContent = 'Kapcsolódás...';
    
    try {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload', true);
        
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 100);
                uploadProgressFill.style.width = `${percent}%`;
                uploadProgressStatus.textContent = `Feltöltés folyamatban... ${percent}%`;
            }
        };
        
        xhr.onload = () => {
            uploadProgressContainer.style.display = 'none';
            if (xhr.status === 200) {
                fileInput.value = ''; // Reset file input
                fetchLibraryAndQueue();
            } else {
                let errText = 'Hiba történt a feltöltés során!';
                try {
                    const resJson = JSON.parse(xhr.responseText);
                    errText = resJson.detail || errText;
                } catch(e){}
                alert(errText);
            }
        };
        
        xhr.onerror = () => {
            uploadProgressContainer.style.display = 'none';
            alert('Hálózati hiba a feltöltés során!');
        };
        
        xhr.send(formData);
        
    } catch (e) {
        uploadProgressContainer.style.display = 'none';
        console.error(e);
        alert('Hiba történt a feltöltés közben!');
    }
}

// -------------------------------------------------------------
// TIMER LOOPS INITIALIZATION
// -------------------------------------------------------------
// 25 FPS update for the simulated facade preview
setInterval(updateFrameAndStatus, 40);

// Fetch queue and library data every 3 seconds
setInterval(fetchLibraryAndQueue, 3000);

// Initial fetches
fetchLibraryAndQueue();
updateFrameAndStatus();

// -------------------------------------------------------------
// LIVE RELOAD
// -------------------------------------------------------------
function initLiveReload() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/live-reload/ws`;
    const socket = new WebSocket(wsUrl);

    socket.onmessage = (event) => {
        if (event.data === 'reload') {
            console.log('Live reload signal received, reloading page...');
            window.location.reload();
        }
    };

    socket.onclose = () => {
        // Try to reconnect every 2 seconds if connection closes
        setTimeout(initLiveReload, 2000);
    };

    socket.onerror = () => {
        socket.close();
    };
}

initLiveReload();
