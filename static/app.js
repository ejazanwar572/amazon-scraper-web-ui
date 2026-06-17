const state = {
    currentTab: 'dashboard',
    scrapingActive: false,
    pollInterval: null,
    products: {
        list: [],
        page: 1,
        limit: 50,
        totalPages: 1,
        searchQuery: '',
        sortBy: 'scraped_at',
        sortOrder: 'desc',
        searchTimeout: null
    },
    alerts: {
        list: [],
        page: 1,
        limit: 50,
        totalPages: 1,
        searchQuery: '',
        minChange: 30,
        searchTimeout: null
    }
};

// ---------------------------------------------------------------------------
// Lifecycle & Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    // Initial fetch of statistics and product catalog
    refreshAllData();
    
    // Start background status polling
    startStatusPolling(2500);
});

function refreshAllData() {
    fetchStats();
    fetchProducts();
}

// ---------------------------------------------------------------------------
// Tab Management
// ---------------------------------------------------------------------------
function switchTab(tabId) {
    state.currentTab = tabId;
    
    // Toggle active classes on side nav links
    document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById(`tab-${tabId}-btn`).classList.add('active');
    
    // Toggle active classes on tab panels
    document.querySelectorAll('.tab-pane').forEach(panel => panel.classList.remove('active'));
    document.getElementById(`tab-${tabId}`).classList.add('active');
    
    // Update Page Header Info
    const pageTitle = document.getElementById('page-title');
    const pageSubtitle = document.getElementById('page-subtitle');
    
    if (tabId === 'dashboard') {
        pageTitle.innerText = 'Scraper Dashboard';
        pageSubtitle.innerText = 'Real-time control panel, background tasks monitor, and stats aggregator.';
        fetchStats();
    } else if (tabId === 'database') {
        pageTitle.innerText = 'Products Catalog';
        pageSubtitle.innerText = 'Query, search, sort, and analyze scraped product listings in PostgreSQL.';
        fetchProducts();
    } else if (tabId === 'alerts') {
        pageTitle.innerText = 'Price Change Alerts';
        pageSubtitle.innerText = 'Monitor products with major price falls or hikes over time (>= 30% threshold).';
        fetchPriceAlerts();
    }
}

// ---------------------------------------------------------------------------
// API Interaction - Scraper Tasks
// ---------------------------------------------------------------------------
async function triggerScrape(event) {
    event.preventDefault();
    if (state.scrapingActive) return;

    const keywordInput = document.getElementById('keyword-input');
    const pagesSelect = document.getElementById('pages-select');
    const marketplaceSelect = document.getElementById('marketplace-select');
    const submitBtn = document.getElementById('start-scrape-btn');
    
    const keyword = keywordInput.value.trim();
    if (!keyword) return;
    
    const payload = {
        keyword: keyword,
        max_pages: parseInt(pagesSelect.value),
        marketplace: marketplaceSelect.value
    };

    // Lock Submit button
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Initializing...';
    
    try {
        const response = await fetch('/api/scrape', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        const data = await response.json();
        
        if (response.ok) {
            appendLog('info', `SCRAPE COMMAND ISSUED: Started background crawl for '${keyword}'...`);
            
            // Re-activate fast polling
            state.scrapingActive = true;
            startStatusPolling(1000);
            
            // Clear input field
            keywordInput.value = '';
        } else {
            appendLog('error', `FAILED TO START SCRAPE: ${data.detail || 'Unknown error'}`);
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i class="fa-solid fa-circle-play"></i> Start Scraping Task';
        }
    } catch (err) {
        appendLog('error', `SERVER CONNECT ERROR: ${err.message}`);
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-circle-play"></i> Start Scraping Task';
    }
}

// ---------------------------------------------------------------------------
// API Interaction - Scraper Status & Logging Polling
// ---------------------------------------------------------------------------
function startStatusPolling(intervalMs) {
    if (state.pollInterval) clearInterval(state.pollInterval);
    
    pollStatus();
    state.pollInterval = setInterval(pollStatus, intervalMs);
}

async function pollStatus() {
    try {
        const response = await fetch('/api/status');
        if (!response.ok) return;
        
        const data = await response.json();
        
        // Check state transition (active/idle)
        if (data.active !== state.scrapingActive) {
            state.scrapingActive = data.active;
            // Switch polling interval based on active state (1s vs 3s)
            startStatusPolling(data.active ? 1000 : 3500);
            
            if (!data.active) {
                // Scraping just finished, sync dashboard
                refreshAllData();
            }
        }
        
        updateMonitorUI(data);
    } catch (err) {
        console.error('Polling status failed:', err);
    }
}

function updateMonitorUI(data) {
    const submitBtn = document.getElementById('start-scrape-btn');
    const widgetDot = document.getElementById('widget-status-dot');
    const widgetTitle = document.getElementById('widget-status-title');
    const widgetDesc = document.getElementById('widget-status-desc');
    const monitorBadge = document.getElementById('monitor-badge');
    
    const progressContainer = document.getElementById('monitor-progress-container');
    const placeholder = document.getElementById('monitor-placeholder-idle');
    
    const triggerPriceCheckBtn = document.getElementById('trigger-price-check-btn');
    
    if (data.active) {
        // Scraper is active
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<i class="fa-solid fa-triangle-exclamation fa-beat text-orange"></i> Scraping in progress...';
        
        if (triggerPriceCheckBtn) {
            triggerPriceCheckBtn.disabled = true;
            triggerPriceCheckBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Scraping Prices...';
        }
        
        widgetDot.classList.add('active');
        widgetTitle.innerText = 'Scraping';
        widgetDesc.innerText = `Keyword: ${data.keyword}`;
        
        monitorBadge.className = 'badge badge-active';
        monitorBadge.innerText = 'Running';
        
        placeholder.classList.add('hidden');
        progressContainer.classList.remove('hidden');
        
        // Update progress metadata
        document.getElementById('progress-keyword').innerText = `Keyword: ${data.keyword}`;
        
        const processed = data.done + data.failed;
        const total = data.total;
        const percent = total > 0 ? Math.round((processed / total) * 100) : 0;
        
        document.getElementById('progress-percent-text').innerText = `${percent}%`;
        document.getElementById('progress-bar-fill').style.width = `${percent}%`;
        
        document.getElementById('metric-scraped').innerText = processed;
        document.getElementById('metric-saved').innerText = data.saved;
        document.getElementById('metric-failed').innerText = data.failed;
        
        if (data.current_asin) {
            document.getElementById('progress-current-asin-area').classList.remove('hidden');
            document.getElementById('current-asin-val').innerText = data.current_asin;
        } else {
            document.getElementById('progress-current-asin-area').classList.add('hidden');
        }
    } else {
        // Scraper is idle
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-circle-play"></i> Start Scraping Task';
        
        if (triggerPriceCheckBtn) {
            triggerPriceCheckBtn.disabled = false;
            triggerPriceCheckBtn.innerHTML = '<i class="fa-solid fa-arrows-spin"></i> Scrape Latest Prices';
        }
        
        widgetDot.classList.remove('active');
        widgetTitle.innerText = 'Idle';
        widgetDesc.innerText = 'Ready to scrape';
        
        monitorBadge.className = 'badge badge-idle';
        monitorBadge.innerText = 'Idle';
        
        placeholder.classList.remove('hidden');
        progressContainer.classList.add('hidden');
    }
    
    // Render Last Scrape Summary Card (if details exist)
    const lastScrapePanel = document.getElementById('last-scrape-summary-panel');
    if (data.last_scrape && data.last_scrape.keyword) {
        lastScrapePanel.classList.remove('hidden');
        document.getElementById('last-keyword').innerText = data.last_scrape.keyword;
        document.getElementById('last-keyword').title = data.last_scrape.keyword;
        document.getElementById('last-saved').innerText = data.last_scrape.products_saved;
        document.getElementById('last-items-per-page').innerText = data.last_scrape.avg_products_per_page;
        document.getElementById('last-failed').innerText = data.last_scrape.failed_runs;
        document.getElementById('last-success-rate').innerText = `${data.last_scrape.success_rate}%`;
        document.getElementById('last-duration').innerText = `${data.last_scrape.duration_seconds}s`;
    } else {
        lastScrapePanel.classList.add('hidden');
    }
    
    // Render rolling log records
    renderLogs(data.logs);
}

function renderLogs(logsList) {
    const consoleLogs = document.getElementById('console-logs-output');
    if (!logsList || logsList.length === 0) return;
    
    // Quick diff check to avoid rewriting console if nothing changed
    const currentLinesCount = consoleLogs.children.length;
    if (currentLinesCount === logsList.length) return;
    
    const wasScrolledToBottom = consoleLogs.scrollHeight - consoleLogs.clientHeight <= consoleLogs.scrollTop + 5;
    
    consoleLogs.innerHTML = '';
    logsList.forEach(line => {
        const div = document.createElement('div');
        div.className = 'log-line';
        
        if (line.includes('[WARNING]') || line.includes('Block detected') || line.includes('CAPTCHA')) {
            div.className += ' log-warn';
        } else if (line.includes('[ERROR]') || line.includes('failed') || line.includes('crashed')) {
            div.className += ' log-error';
        } else if (line.includes('finished successfully') || line.includes('Saved product') || line.includes('Products Saved')) {
            div.className += ' log-success';
        } else {
            div.className += ' log-info';
        }
        
        div.innerText = line;
        consoleLogs.appendChild(div);
    });
    
    // Auto Scroll to bottom if user was previously looking at latest log
    if (wasScrolledToBottom) {
        consoleLogs.scrollTop = consoleLogs.scrollHeight;
    }
}

async function clearConsole() {
    try {
        await fetch('/api/logs/clear', { method: 'POST' });
    } catch (err) {
        console.error('Failed to clear logs on server:', err);
    }
    document.getElementById('console-logs-output').innerHTML = '<div class="log-line log-info">Console cleared.</div>';
}

function appendLog(level, message) {
    const consoleLogs = document.getElementById('console-logs-output');
    const time = new Date().toTimeString().split(' ')[0];
    const line = `${time} [${level.toUpperCase()}] ${message}`;
    
    const div = document.createElement('div');
    div.className = `log-line log-${level}`;
    div.innerText = line;
    consoleLogs.appendChild(div);
    consoleLogs.scrollTop = consoleLogs.scrollHeight;
}

// ---------------------------------------------------------------------------
// API Interaction - Statistics & KPI Aggregates
// ---------------------------------------------------------------------------
async function fetchStats() {
    try {
        const response = await fetch('/api/stats');
        if (!response.ok) return;
        
        const data = await response.json();
        
        // Update KPIs
        document.getElementById('kpi-total-products').innerText = data.total_products.toLocaleString();
        document.getElementById('kpi-avg-price').innerText = `₹${data.avg_price.toLocaleString()}`;
        document.getElementById('kpi-unique-brands').innerText = data.unique_brands.toLocaleString();
        document.getElementById('kpi-unique-sellers').innerText = data.unique_sellers.toLocaleString();
        
        // Render Top Brands representation bar list
        renderTopBrands(data.top_brands, data.total_products);
        
        // Render Marketplace flags distribution
        renderMarketplaceStats(data.marketplaces);
    } catch (err) {
        console.error('Failed to fetch stats:', err);
    }
}

function renderTopBrands(brands, total) {
    const listContainer = document.getElementById('top-brands-list');
    listContainer.innerHTML = '';
    
    if (!brands || brands.length === 0) {
        listContainer.innerHTML = '<div class="empty-state">No brand stats available.</div>';
        return;
    }
    
    // Find max count to normalize bar scale relative to top brand instead of total database records
    const maxVal = brands[0].count;
    
    brands.forEach(item => {
        const percent = maxVal > 0 ? (item.count / maxVal) * 100 : 0;
        
        const row = document.createElement('div');
        row.className = 'brand-row';
        row.innerHTML = `
            <div class="brand-name" title="${item.brand}">${item.brand}</div>
            <div class="brand-bar-container">
                <div class="brand-bar-fill" style="width: ${percent}%"></div>
            </div>
            <div class="brand-count">${item.count}</div>
        `;
        listContainer.appendChild(row);
    });
}

function renderMarketplaceStats(marketplaces) {
    const container = document.getElementById('marketplace-stats-list');
    container.innerHTML = '';
    
    const mkMap = Object.entries(marketplaces);
    if (mkMap.length === 0) {
        container.innerHTML = '<div class="empty-state">No marketplace distribution available.</div>';
        return;
    }
    
    mkMap.forEach(([tld, count]) => {
        // Map country flag emoji
        let flag = '🌐';
        if (tld === 'in') flag = '🇮🇳';
        else if (tld === 'com') flag = '🇺🇸';
        else if (tld === 'co.uk' || tld === 'uk') flag = '🇬🇧';
        
        const card = document.createElement('div');
        card.className = 'market-stat-card';
        card.innerHTML = `
            <div class="market-flag">${flag} ${tld}</div>
            <div class="market-count">${count}</div>
            <div class="market-label">Products Saved</div>
        `;
        container.appendChild(card);
    });
}

// ---------------------------------------------------------------------------
// API Interaction - Catalog Products Browser Table
// ---------------------------------------------------------------------------
async function fetchProducts() {
    const tbody = document.getElementById('products-table-body');
    if (state.currentTab !== 'database' && tbody.children.length > 0) return; // avoid redundant fetch if active page is not db
    
    renderTableSkeletons();
    
    const p = state.products;
    let url = `/api/products?page=${p.page}&limit=${p.limit}&sort_by=${p.sortBy}&sort_order=${p.sortOrder}`;
    
    if (p.searchQuery) {
        url += `&search=${encodeURIComponent(p.searchQuery)}`;
    }
    
    try {
        const response = await fetch(url);
        if (!response.ok) {
            tbody.innerHTML = '<tr><td colspan="11" class="empty-state text-red">Failed to load products.</td></tr>';
            return;
        }
        
        const data = await response.json();
        p.list = data.products;
        p.totalPages = data.pagination.total_pages;
        
        renderProductsTable(data.products);
        renderPagination(data.pagination);
    } catch (err) {
        console.error('Failed to fetch products:', err);
        tbody.innerHTML = '<tr><td colspan="11" class="empty-state text-red">Error connecting to server.</td></tr>';
    }
}

function renderTableSkeletons() {
    const tbody = document.getElementById('products-table-body');
    tbody.innerHTML = '';
    
    for (let i = 0; i < 5; i++) {
        const tr = document.createElement('tr');
        tr.className = 'skeleton-row';
        tr.innerHTML = `
            <td><div class="skeleton-text" style="width: 36px; height: 36px; border-radius: 4px;"></div></td>
            <td><div class="skeleton-text" style="width: 70px;"></div></td>
            <td><div class="skeleton-text" style="width: 100%;"></div></td>
            <td><div class="skeleton-text" style="width: 80px;"></div></td>
            <td><div class="skeleton-text" style="width: 50px;"></div></td>
            <td><div class="skeleton-text" style="width: 60px;"></div></td>
            <td><div class="skeleton-text" style="width: 40px;"></div></td>
            <td><div class="skeleton-text" style="width: 50px;"></div></td>
            <td><div class="skeleton-text" style="width: 90px;"></div></td>
            <td><div class="skeleton-text" style="width: 80px;"></div></td>
            <td><div class="skeleton-text" style="width: 20px;"></div></td>
        `;
        tbody.appendChild(tr);
    }
}

function renderProductsTable(products) {
    const tbody = document.getElementById('products-table-body');
    tbody.innerHTML = '';
    
    if (!products || products.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" class="empty-state">No products found matching the criteria.</td></tr>';
        return;
    }
    
    products.forEach(item => {
        const tr = document.createElement('tr');
        
        // Formatting price
        const formattedPrice = item.price 
            ? `${item.currency === 'INR' ? '₹' : item.currency + ' '}${item.price.toLocaleString()}`
            : 'Unavailable';
            
        // Formatting rating
        const formattedRating = item.rating 
            ? `<span><i class="fa-solid fa-star" style="color: gold; margin-right: 4px;"></i>${item.rating}</span>`
            : '-';
            
        // Formatting date
        const dateStr = item.scraped_at 
            ? new Date(item.scraped_at).toLocaleDateString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})
            : '-';

        // Thumb image (use placeholder if missing)
        const imgSrc = item.image_url || 'https://images-na.ssl-images-amazon.com/images/I/01RmK7tJpGL._AC_SY200_.jpg';
        
        tr.innerHTML = `
            <td class="thumbnail-cell">
                <img src="${imgSrc}" class="product-thumb" alt="Product thumbnail" onerror="this.src='https://images-na.ssl-images-amazon.com/images/I/01RmK7tJpGL._AC_SY200_.jpg'">
            </td>
            <td><span class="badge-pill badge-pill-idle" style="font-family: monospace;">${item.asin}</span></td>
            <td title="${item.title}"><div class="title-cell-text">${item.title || '-'}</div></td>
            <td><strong>${item.brand || '-'}</strong></td>
            <td><span class="badge-pill badge-pill-spec">${item.specification || '-'}</span></td>
            <td><span class="badge-pill badge-pill-price">${formattedPrice}</span></td>
            <td><span class="badge-pill badge-pill-rating">${formattedRating}</span></td>
            <td>${item.review_count ? item.review_count.toLocaleString() : '-'}</td>
            <td><div class="title-cell-text" style="max-width: 120px;" title="${item.seller || ''}">${item.seller || '-'}</div></td>
            <td>${dateStr}</td>
            <td>
                <a href="${item.url}" target="_blank" class="db-table-link" title="Open product page in Amazon"><i class="fa-solid fa-up-right-from-square"></i></a>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function renderPagination(meta) {
    const info = document.getElementById('pagination-info-text');
    const controls = document.getElementById('pagination-controls-buttons');
    
    if (meta.total_records === 0) {
        info.innerText = 'Showing 0 to 0 of 0 entries';
        controls.innerHTML = `
            <button class="page-btn" disabled><i class="fa-solid fa-angle-left"></i> Previous</button>
            <span class="active-page-num">0</span>
            <button class="page-btn" disabled>Next <i class="fa-solid fa-angle-right"></i></button>
        `;
        return;
    }
    
    const start = (meta.page - 1) * meta.limit + 1;
    const end = Math.min(meta.page * meta.limit, meta.total_records);
    
    info.innerText = `Showing ${start} to ${end} of ${meta.total_records} entries`;
    
    const prevDisabled = meta.page <= 1 ? 'disabled' : '';
    const nextDisabled = meta.page >= meta.total_pages ? 'disabled' : '';
    
    controls.innerHTML = `
        <button class="page-btn" ${prevDisabled} onclick="changePage(${meta.page - 1})">
            <i class="fa-solid fa-angle-left"></i> Previous
        </button>
        <span class="active-page-num">${meta.page} / ${meta.total_pages}</span>
        <button class="page-btn" ${nextDisabled} onclick="changePage(${meta.page + 1})">
            Next <i class="fa-solid fa-angle-right"></i>
        </button>
    `;
}

function changePage(newPage) {
    if (newPage < 1 || newPage > state.products.totalPages) return;
    state.products.page = newPage;
    fetchProducts();
}

function handleSearchInput() {
    // Clear debounce timer
    if (state.products.searchTimeout) {
        clearTimeout(state.products.searchTimeout);
    }
    
    // Set 400ms debounce
    state.products.searchTimeout = setTimeout(() => {
        state.products.searchQuery = document.getElementById('db-search-input').value.trim();
        state.products.page = 1; // reset to page 1
        fetchProducts();
    }, 400);
}

// ---------------------------------------------------------------------------
// Price Alerts & Check Functionality
// ---------------------------------------------------------------------------
async function fetchPriceAlerts() {
    const tbody = document.getElementById('alerts-table-body');
    if (!tbody) return;
    
    const thresholdSelect = document.getElementById('alerts-threshold-select');
    if (thresholdSelect) {
        state.alerts.minChange = parseFloat(thresholdSelect.value);
    }
    
    tbody.innerHTML = '';
    for (let i = 0; i < 5; i++) {
        tbody.innerHTML += `
            <tr>
                <td><div class="skeleton-text" style="width: 50px; height: 50px; border-radius: 4px;"></div></td>
                <td><div class="skeleton-text" style="width: 70px;"></div></td>
                <td><div class="skeleton-text" style="width: 100%;"></div></td>
                <td><div class="skeleton-text" style="width: 80px;"></div></td>
                <td><div class="skeleton-text" style="width: 50px;"></div></td>
                <td><div class="skeleton-text" style="width: 60px;"></div></td>
                <td><div class="skeleton-text" style="width: 60px;"></div></td>
                <td><div class="skeleton-text" style="width: 60px;"></div></td>
                <td><div class="skeleton-text" style="width: 80px;"></div></td>
                <td><div class="skeleton-text" style="width: 30px;"></div></td>
            </tr>
        `;
    }
    
    let url = `/api/price-alerts?page=${state.alerts.page}&limit=${state.alerts.limit}&min_change=${state.alerts.minChange}`;
    if (state.alerts.searchQuery) {
        url += `&search=${encodeURIComponent(state.alerts.searchQuery)}`;
    }
    
    try {
        const response = await fetch(url);
        if (!response.ok) {
            tbody.innerHTML = '<tr><td colspan="10" class="empty-state text-red">Failed to load price alerts.</td></tr>';
            return;
        }
        
        const data = await response.json();
        state.alerts.list = data.alerts;
        state.alerts.totalPages = data.pagination.total_pages;
        
        renderAlertsTable(data.alerts);
        renderAlertsPagination(data.pagination);
    } catch (err) {
        console.error('Failed to fetch price alerts:', err);
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state text-red">Error connecting to server.</td></tr>';
    }
}

function renderAlertsTable(alerts) {
    const tbody = document.getElementById('alerts-table-body');
    if (!tbody) return;
    
    tbody.innerHTML = '';
    
    if (!alerts || alerts.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state">No price alerts found matching the criteria.</td></tr>';
        return;
    }
    
    alerts.forEach(item => {
        const tr = document.createElement('tr');
        
        const formatPrice = (val) => val 
            ? `${item.currency === 'INR' ? '₹' : item.currency + ' '}${val.toLocaleString()}`
            : 'Unavailable';
            
        const formattedInitialPrice = formatPrice(item.initial_price);
        const formattedCurrentPrice = formatPrice(item.price);
        
        let changeBadge = '';
        const pct = item.change_percent;
        if (pct !== null) {
            const displayPct = Math.abs(pct).toFixed(1) + '%';
            if (pct < 0) {
                changeBadge = `<span class="badge-pill badge-price-drop"><i class="fa-solid fa-arrow-trend-down"></i> ${displayPct}</span>`;
            } else {
                changeBadge = `<span class="badge-pill badge-price-hike"><i class="fa-solid fa-arrow-trend-up"></i> ${displayPct}</span>`;
            }
        } else {
            changeBadge = '-';
        }
            
        const dateStr = item.scraped_at 
            ? new Date(item.scraped_at).toLocaleDateString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})
            : '-';

        const imgSrc = item.image_url || 'https://images-na.ssl-images-amazon.com/images/I/01RmK7tJpGL._AC_SY200_.jpg';
        
        tr.innerHTML = `
            <td class="thumbnail-cell">
                <img src="${imgSrc}" class="product-thumb" alt="Product thumbnail" onerror="this.src='https://images-na.ssl-images-amazon.com/images/I/01RmK7tJpGL._AC_SY200_.jpg'">
            </td>
            <td><span class="badge-pill badge-pill-idle" style="font-family: monospace;">${item.asin}</span></td>
            <td title="${item.title}"><div class="title-cell-text">${item.title || '-'}</div></td>
            <td><strong>${item.brand || '-'}</strong></td>
            <td><span class="badge-pill badge-pill-spec">${item.specification || '-'}</span></td>
            <td><span class="badge-pill badge-pill-price" style="background: hsla(220, 15%, 25%, 0.3); border-color: hsla(220, 15%, 25%, 0.5);">${formattedInitialPrice}</span></td>
            <td><span class="badge-pill badge-pill-price">${formattedCurrentPrice}</span></td>
            <td>${changeBadge}</td>
            <td>${dateStr}</td>
            <td>
                <a href="${item.url}" target="_blank" class="db-table-link" title="Open product page in Amazon"><i class="fa-solid fa-up-right-from-square"></i></a>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function renderAlertsPagination(meta) {
    const info = document.getElementById('alerts-pagination-info-text');
    const controls = document.getElementById('alerts-pagination-controls-buttons');
    if (!info || !controls) return;
    
    if (meta.total_records === 0) {
        info.innerText = 'Showing 0 to 0 of 0 entries';
        controls.innerHTML = `
            <button class="page-btn" disabled><i class="fa-solid fa-angle-left"></i> Previous</button>
            <span class="active-page-num">0</span>
            <button class="page-btn" disabled>Next <i class="fa-solid fa-angle-right"></i></button>
        `;
        return;
    }
    
    const start = (meta.page - 1) * meta.limit + 1;
    const end = Math.min(meta.page * meta.limit, meta.total_records);
    
    info.innerText = `Showing ${start} to ${end} of ${meta.total_records} entries`;
    
    const prevDisabled = meta.page <= 1 ? 'disabled' : '';
    const nextDisabled = meta.page >= meta.total_pages ? 'disabled' : '';
    
    controls.innerHTML = `
        <button class="page-btn" ${prevDisabled} onclick="changeAlertsPage(${meta.page - 1})">
            <i class="fa-solid fa-angle-left"></i> Previous
        </button>
        <span class="active-page-num">${meta.page} / ${meta.total_pages}</span>
        <button class="page-btn" ${nextDisabled} onclick="changeAlertsPage(${meta.page + 1})">
            Next <i class="fa-solid fa-angle-right"></i>
        </button>
    `;
}

function changeAlertsPage(newPage) {
    if (newPage < 1 || newPage > state.alerts.totalPages) return;
    state.alerts.page = newPage;
    fetchPriceAlerts();
}

function handleAlertsSearchInput() {
    if (state.alerts.searchTimeout) {
        clearTimeout(state.alerts.searchTimeout);
    }
    
    state.alerts.searchTimeout = setTimeout(() => {
        state.alerts.searchQuery = document.getElementById('alerts-search-input').value.trim();
        state.alerts.page = 1;
        fetchPriceAlerts();
    }, 400);
}

async function triggerPriceCheck() {
    if (state.scrapingActive) return;
    
    const btn = document.getElementById('trigger-price-check-btn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Scraping Prices...`;
    }
    
    try {
        const response = await fetch('/api/check-prices', { method: 'POST' });
        if (!response.ok) {
            const data = await response.json();
            alert(`Failed to start price scrape: ${data.detail || 'Unknown error'}`);
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = `<i class="fa-solid fa-arrows-spin"></i> Scrape Latest Prices`;
            }
            return;
        }
        
        console.log("Price check background task triggered successfully");
    } catch (err) {
        console.error("Failed to trigger price check:", err);
        alert("Network error starting price scrape");
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<i class="fa-solid fa-arrows-spin"></i> Scrape Latest Prices`;
        }
    }
}
