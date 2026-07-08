import { Outlet } from "react-router-dom";
import { SidebarNav } from "./SidebarNav";

/**
 * Shared layout that keeps the navigation sidebar visible while the routed page
 * renders in the main area. Used for Settings, Personas, Snippets, etc.
 */
export function AppLayout() {
  return (
    <div className="flex h-full bg-white dark:bg-gray-950">
      <SidebarNav />
      <div className="flex min-w-0 flex-1 flex-col">
        <Outlet />
      </div>
    </div>
  );
}
