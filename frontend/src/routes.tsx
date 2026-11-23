import type { RouteObject } from "react-router";

import App from "@/App";
import { AdminAccess } from "@/components/app/AdminAccess";
import { RouteError } from "@/components/app/RouteError";

export const routes: RouteObject[] = [
  {
    path: "*",
    element: (
      <AdminAccess>
        <App />
      </AdminAccess>
    ),
    errorElement: <RouteError />,
  },
];
