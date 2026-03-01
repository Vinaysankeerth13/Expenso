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

// Chart.js global defaults
Chart.defaults.font.family = 'system-ui, -apple-system, sans-serif';
Chart.defaults.font.size = 12;
Chart.defaults.color = '#6B7280';

// --- Inline category editor ---

function toggleCategoryDropdown(btn) {
    const dropdown = btn.nextElementSibling;
    const wasHidden = dropdown.classList.contains('hidden');

    // Close all open dropdowns first
    document.querySelectorAll('.category-dropdown').forEach(d => d.classList.add('hidden'));

    if (wasHidden) {
        dropdown.classList.remove('hidden');
    }
}

// Close dropdown when clicking outside
document.addEventListener('click', function(e) {
    if (!e.target.closest('.category-editor')) {
        document.querySelectorAll('.category-dropdown').forEach(d => d.classList.add('hidden'));
    }
});

// Close dropdown after HTMX swap (category was updated)
document.addEventListener('htmx:afterSwap', function() {
    document.querySelectorAll('.category-dropdown').forEach(d => d.classList.add('hidden'));
});
