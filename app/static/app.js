/**
 * Expenso Dashboard - Shared utilities
 */

// Format number in Indian numbering (1,23,456)
function formatINR(value) {
    if (value === null || value === undefined) return '0';
    const num = Math.abs(Math.round(value));
    const s = num.toString();
    if (s.length > 3) {
        const last3 = s.slice(-3);
        let rest = s.slice(0, -3);
        const groups = [];
        while (rest.length > 0) {
            groups.unshift(rest.slice(-2));
            rest = rest.slice(0, -2);
        }
        return groups.join(',') + ',' + last3;
    }
    return s;
}

function toggleNav(btn) {
    const open = document.getElementById('site-nav').classList.toggle('is-open');
    btn.setAttribute('aria-expanded', String(open));
}

// Chart.js defaults matched to the design tokens in app.css
const THEME = {
    ink: '#1B1A17',
    muted: '#6E6A61',
    grid: '#ECE8DF',
    bar: '#D8D2C4',
    dark: '#181712',
    inflow: '#BFE3DC',
};
Chart.defaults.font.family = "'Work Sans', system-ui, sans-serif";
Chart.defaults.font.size = 12;
Chart.defaults.color = THEME.muted;
Chart.defaults.plugins.legend.display = false;
Chart.defaults.plugins.tooltip.backgroundColor = THEME.dark;
Chart.defaults.plugins.tooltip.padding = 10;
Chart.defaults.plugins.tooltip.cornerRadius = 8;
Chart.defaults.plugins.tooltip.displayColors = false;
Chart.defaults.plugins.tooltip.callbacks.label = ctx =>
    ctx.raw === null ? 'Coverage unverified' : '₹' + formatINR(ctx.raw);

function barScales() {
    return {
        y: {
            beginAtZero: true,
            border: { display: false },
            grid: { color: THEME.grid },
            ticks: { callback: v => formatINR(v), maxTicksLimit: 5 },
        },
        x: {
            border: { color: THEME.grid },
            grid: { display: false },
            ticks: { maxRotation: 0, autoSkipPadding: 8 },
        },
    };
}

// --- Inline category editor ---

function closeCategoryDropdowns() {
    document.querySelectorAll('.category-dropdown').forEach(d => d.classList.add('hidden'));
    document.querySelectorAll('.category-editor > button').forEach(b => b.setAttribute('aria-expanded', 'false'));
}

function toggleCategoryDropdown(btn) {
    const dropdown = btn.nextElementSibling;
    const wasHidden = dropdown.classList.contains('hidden');
    closeCategoryDropdowns();
    if (wasHidden) {
        dropdown.classList.remove('hidden');
        btn.setAttribute('aria-expanded', 'true');
        // Fixed placement avoids clipping by the transaction list container.
        const rect = btn.getBoundingClientRect();
        const roomBelow = innerHeight - rect.bottom - 12;
        const roomAbove = rect.top - 12;
        const below = roomBelow >= Math.min(dropdown.scrollHeight, 300) || roomBelow >= roomAbove;
        dropdown.style.position = 'fixed';
        dropdown.style.marginTop = '0';
        dropdown.style.maxHeight = Math.max(80, below ? roomBelow : roomAbove) + 'px';
        dropdown.style.overflowY = 'auto';
        dropdown.style.left = Math.max(8, Math.min(rect.left, innerWidth - dropdown.offsetWidth - 8)) + 'px';
        dropdown.style.top = (below ? rect.bottom + 4 : rect.top - dropdown.offsetHeight - 4) + 'px';
    }
}

document.addEventListener('click', function(e) {
    if (!e.target.closest('.category-editor')) closeCategoryDropdowns();
});
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    const button = document.querySelector('.category-editor > button[aria-expanded="true"]');
    closeCategoryDropdowns();
    if (button) button.focus();
});
window.addEventListener('resize', closeCategoryDropdowns);
window.addEventListener('scroll', closeCategoryDropdowns);
document.addEventListener('htmx:afterSwap', closeCategoryDropdowns);

// Keep the current row and explain failed edits/undo instead of losing controls.
document.addEventListener('htmx:responseError', function(event) {
    const row = event.detail.elt.closest('[id^="txn-"]');
    if (!row) return;
    const error = row.querySelector('.classification-error');
    if (error) error.textContent = event.detail.xhr.status === 409
        ? 'This edit has changed. Reload before trying again.'
        : 'Category change failed. Please try again.';
});
