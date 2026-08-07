// Loaded as a blocking <script src> in <head> (no defer/async) so this runs
// before the sidebar paints — applying the collapsed state here, rather
// than after DOMContentLoaded, avoids a flash of the expanded sidebar on
// every page load.
if (localStorage.getItem('folderw_sidebar_collapsed') === '1') {
    document.documentElement.classList.add('sidebar-collapsed');
}

function toggleSidebar() {
    const collapsed = document.documentElement.classList.toggle('sidebar-collapsed');
    localStorage.setItem('folderw_sidebar_collapsed', collapsed ? '1' : '0');
}
