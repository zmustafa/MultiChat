import { NavLink, Outlet } from "react-router";
import { SidebarNav } from "./SidebarNav";

const MOBILE_SECTIONS = [
  ["/settings", "Providers", true],
  ["/personas", "Personas", true],
  ["/snippets", "Snippets", true],
  ["/analytics", "Usage", true],
  ["/evals", "Evals", true],
  ["/integrations", "Integrations", true],
  ["/settings/general", "General", false],
  ["/settings/security", "Security", false],
] as const;

/**
 * Shared layout that keeps the navigation sidebar visible while the routed page
 * renders in the main area. Used for Settings, Personas, Snippets, etc.
 */
export function AppLayout() {
  return (
    <div className="flex h-full bg-white dark:bg-gray-950">
      <div className="hidden lg:flex">
        <SidebarNav />
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <nav
          aria-label="App sections"
          className="mobile-scrollbar-hidden flex shrink-0 gap-1 overflow-x-auto border-b border-gray-200 bg-gray-50 px-2 py-1.5 lg:hidden dark:border-gray-700 dark:bg-gray-950"
        >
          {MOBILE_SECTIONS.map(([to, label, end]) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `shrink-0 rounded-full border px-3 py-1 text-xs font-medium ${
                  isActive
                    ? "border-brand bg-brand/10 text-brand"
                    : "border-gray-300 bg-white text-gray-600 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-300"
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
        <Outlet />
      </div>
    </div>
  );
}
