// Initialize Telegram WebApp
let tg = null;
if (window.Telegram && window.Telegram.WebApp) {
    tg = window.Telegram.WebApp;
    tg.ready();
    tg.expand();
    
    // Notify body to use Telegram theme colors if applicable
    document.body.classList.add('telegram-theme');
}

// State variables
let currentVideoData = null;

// DOM Elements
const videoUrlInput = document.getElementById('video-url');
const pasteBtn = document.getElementById('paste-btn');
const extractBtn = document.getElementById('extract-btn');
const loader = document.getElementById('loader');
const previewCard = document.getElementById('preview-card');
const videoThumbnail = document.getElementById('video-thumbnail');
const videoDuration = document.getElementById('video-duration');
const videoTitle = document.getElementById('video-title');
const videoPlatform = document.getElementById('video-platform');
const platformOverlay = document.getElementById('platform-overlay');
const formatSelect = document.getElementById('format-select');
const downloadBtn = document.getElementById('download-btn');
const progressContainer = document.getElementById('progress-container');
const progressBar = document.getElementById('progress-bar');
const progressPercent = document.getElementById('progress-percent');
const historyList = document.getElementById('history-list');
const clearHistoryBtn = document.getElementById('clear-history');
const toast = document.getElementById('toast');
const toastMessage = document.getElementById('toast-message');

// Event Listeners
pasteBtn.addEventListener('click', handlePaste);
extractBtn.addEventListener('click', handleExtract);
downloadBtn.addEventListener('click', handleDownload);
clearHistoryBtn.addEventListener('click', clearHistory);

// Initialize App
loadHistory();

// Functions

/**
 * Paste link from clipboard
 */
async function handlePaste() {
    try {
        const text = await navigator.clipboard.readText();
        if (text) {
            videoUrlInput.value = text;
            showToast("Link pasted from clipboard! 📋");
            vibrateDevice("light");
        } else {
            showToast("Clipboard is empty.");
        }
    } catch (err) {
        showToast("Cannot read clipboard. Please paste manually.");
        console.error("Paste error:", err);
    }
}

/**
 * Handle video metadata extraction
 */
async function handleExtract() {
    const url = videoUrlInput.value.trim();
    if (!url) {
        showToast("Please enter or paste a video link first!");
        return;
    }

    // Hide preview, show loader
    previewCard.classList.add('hidden');
    progressContainer.classList.add('hidden');
    loader.classList.remove('hidden');
    extractBtn.disabled = true;
    vibrateDevice("medium");

    try {
        // Encode URL for query parameter
        const response = await fetch(`/api/extract?url=${encodeURIComponent(url)}`);
        const data = await response.json();

        loader.classList.add('hidden');
        extractBtn.disabled = false;

        if (data.success) {
            currentVideoData = data;
            renderPreviewCard(data);
            showToast("Video extracted successfully! 🎉");
            vibrateDevice("success");
        } else {
            showToast(data.error || "Failed to extract video information.");
            vibrateDevice("error");
        }
    } catch (err) {
        loader.classList.add('hidden');
        extractBtn.disabled = false;
        showToast("Connection error. Is the server running?");
        vibrateDevice("error");
        console.error("Extraction error:", err);
    }
}

/**
 * Format duration helper (sec to mm:ss)
 */
function formatDuration(sec) {
    if (!sec || isNaN(sec)) return "00:00";
    const minutes = Math.floor(sec / 60);
    const seconds = Math.floor(sec % 60);
    return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

/**
 * Render video preview layout
 */
function renderPreviewCard(data) {
    videoTitle.textContent = data.title;
    videoPlatform.textContent = data.platform;
    platformOverlay.textContent = data.platform.substring(0, 3);
    
    // Set thumbnail with fallback
    videoThumbnail.src = data.thumbnail || "https://images.unsplash.com/photo-1611162617213-7d7a39e9b1d7?w=500&auto=format&fit=crop&q=60";
    
    // Set duration
    videoDuration.textContent = formatDuration(data.duration);
    
    // Populate formats dropdown
    formatSelect.innerHTML = "";
    if (data.formats && data.formats.length > 0) {
        data.formats.forEach(f => {
            const sizeStr = f.filesize ? ` (~${(f.filesize / (1024 * 1024)).toFixed(1)}MB)` : '';
            const option = document.createElement('option');
            option.value = f.url;
            option.dataset.formatId = f.format_id;
            option.textContent = `${f.resolution || 'Default Quality'} - .${f.ext}${sizeStr}`;
            formatSelect.appendChild(option);
        });
    } else {
        const option = document.createElement('option');
        option.value = data.video_url;
        option.dataset.formatId = "default";
        option.textContent = "Default Quality - .mp4";
        formatSelect.appendChild(option);
    }
    
    previewCard.classList.remove('hidden');
}

/**
 * Handle Downloading video
 */
function handleDownload() {
    if (!currentVideoData) return;
    
    const selectedUrl = formatSelect.value;
    const title = currentVideoData.title;
    
    if (!selectedUrl) {
        showToast("Download URL not found!");
        return;
    }
    
    vibrateDevice("light");
    
    // Add to history
    addToHistory({
        title: title,
        platform: currentVideoData.platform,
        url: videoUrlInput.value.trim(),
        downloadUrl: selectedUrl,
        timestamp: Date.now()
    });
    
    // Construct direct download endpoint URL
    // Double encoding ensures special characters in URLs are preserved correctly
    const downloadEndpoint = `/api/download?url=${encodeURIComponent(selectedUrl)}&title=${encodeURIComponent(title)}`;
    
    // Open in a new tab or trigger iframe/hidden anchor download to avoid breaking current view
    const link = document.createElement('a');
    link.href = downloadEndpoint;
    link.target = '_blank';
    link.click();
    
    // Show mock progress bar animation to look premium
    showProgressAnimation();
}

/**
 * Mock progress bar animation
 */
function showProgressAnimation() {
    progressContainer.classList.remove('hidden');
    progressBar.style.width = '0%';
    progressPercent.textContent = '0%';
    
    let progress = 0;
    const interval = setInterval(() => {
        progress += Math.floor(Math.random() * 8) + 2;
        if (progress >= 100) {
            progress = 100;
            clearInterval(interval);
            setTimeout(() => {
                progressContainer.classList.add('hidden');
                showToast("Download started in browser! 📥");
            }, 1000);
        }
        progressBar.style.width = `${progress}%`;
        progressPercent.textContent = `${progress}%`;
    }, 150);
}

/**
 * History Management (localStorage)
 */
function loadHistory() {
    const history = JSON.parse(localStorage.getItem('download_history') || '[]');
    historyList.innerHTML = "";
    
    if (history.length === 0) {
        historyList.innerHTML = `<p class="empty-history">Your download history will appear here.</p>`;
        return;
    }
    
    history.forEach((item, index) => {
        const itemEl = document.createElement('div');
        itemEl.className = "history-item";
        itemEl.innerHTML = `
            <div class="history-details">
                <span class="history-item-title" title="${item.title}">${item.title}</span>
                <span class="history-item-platform">${item.platform} • ${new Date(item.timestamp).toLocaleDateString()}</span>
            </div>
            <div class="history-actions">
                <button class="history-action-btn redownload" data-url="${item.url}" title="Load Link"><i class="fa-solid fa-redo"></i></button>
                <button class="history-action-btn delete-item" data-index="${index}" title="Remove"><i class="fa-solid fa-trash"></i></button>
            </div>
        `;
        historyList.appendChild(itemEl);
    });
    
    // Bind buttons inside list
    document.querySelectorAll('.redownload').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const url = e.currentTarget.dataset.url;
            videoUrlInput.value = url;
            handleExtract();
        });
    });
    
    document.querySelectorAll('.delete-item').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const idx = parseInt(e.currentTarget.dataset.index);
            deleteHistoryItem(idx);
        });
    });
}

function addToHistory(item) {
    let history = JSON.parse(localStorage.getItem('download_history') || '[]');
    // Avoid duplicates of the same URL
    history = history.filter(h => h.url !== item.url);
    
    // Insert at beginning
    history.unshift(item);
    
    // Limit to 5 items
    if (history.length > 5) {
        history.pop();
    }
    
    localStorage.setItem('download_history', JSON.stringify(history));
    loadHistory();
}

function deleteHistoryItem(index) {
    let history = JSON.parse(localStorage.getItem('download_history') || '[]');
    history.splice(index, 1);
    localStorage.setItem('download_history', JSON.stringify(history));
    loadHistory();
    showToast("Item removed from history.");
    vibrateDevice("light");
}

function clearHistory() {
    if (confirm("Clear all download history?")) {
        localStorage.removeItem('download_history');
        loadHistory();
        showToast("History cleared.");
        vibrateDevice("light");
    }
}

/**
 * Toast Helper
 */
function showToast(message) {
    toastMessage.textContent = message;
    toast.classList.remove('hidden');
    
    // Clear previous timeouts if click is fast
    if (window.toastTimeout) {
        clearTimeout(window.toastTimeout);
    }
    
    window.toastTimeout = setTimeout(() => {
        toast.classList.add('hidden');
    }, 3000);
}

/**
 * Haptic feedback wrapper for Telegram
 */
function vibrateDevice(style) {
    if (tg && tg.HapticFeedback) {
        if (style === "light" || style === "medium" || style === "heavy") {
            tg.HapticFeedback.impactOccurred(style);
        } else if (style === "success" || style === "warning" || style === "error") {
            tg.HapticFeedback.notificationOccurred(style);
        }
    }
}
