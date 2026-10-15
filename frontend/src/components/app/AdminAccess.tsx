import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, matchPath, useLocation, useNavigate } from "react-router";
import { ApiError, adminSession, SESSION_EXPIRED_EVENT, type AdminSession } from "@/api/client";
import { ErrorAlert, Loading } from "@/components/app/common";
import { UnsavedChangesGuard } from "@/components/app/UnsavedChangesGuard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DialogVisibilityContext } from "@/components/ui/dialog";

const SESSION_KEY = ["admin-session"] as const;
const AdminContext = createContext<{
  session: AdminSession | null;
  endSession: () => Promise<void>;
} | null>(null);

export function useApiKeyRequired(): boolean {
  return useContext(AdminContext)?.session?.api_key_required ?? false;
}

export function AdminAccess({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const location = useLocation();
  const onLogoutRoute = matchPath("/logout", location.pathname) !== null;
  const [keepEditor, setKeepEditor] = useState(false);
  const session = useQuery({
    queryKey: SESSION_KEY,
    queryFn: async () => {
      try {
        return await adminSession("GET");
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    retry: false,
    staleTime: Infinity,
  });
  useEffect(() => {
    try {
      // Remove the API key saved by the old UI; sessions use an HttpOnly cookie.
      localStorage.removeItem("sidewit.apiKey");
    } catch {
      // Cookie login also works when browser storage is unavailable.
    }
  }, []);
  useEffect(() => {
    const expired = () => {
      // Sign-out owns this transition; a background 401 must not offer a competing login.
      if (onLogoutRoute) return;
      // A session timeout hides the editor but keeps its unsaved form state mounted.
      if (qc.getQueryData(SESSION_KEY)) setKeepEditor(true);
      qc.setQueryData(SESSION_KEY, null);
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }, [qc, onLogoutRoute]);

  const signedIn = !!session.data;
  const endSession = async () => {
    await qc.cancelQueries({ predicate: (query) => query.queryKey[0] !== SESSION_KEY[0] });
    qc.removeQueries({ predicate: (query) => query.queryKey[0] !== SESSION_KEY[0] });
    qc.getMutationCache().clear();
    qc.setQueryData(SESSION_KEY, null);
    setKeepEditor(false);
  };
  // Resolve terminal URLs before offering sign-in, independently of router/cache update order.
  if (!signedIn && !session.isPending && onLogoutRoute)
    return <Navigate to="/playground" replace />;
  return (
    <AdminContext.Provider value={{ session: session.data ?? null, endSession }}>
      {(signedIn || keepEditor) && (
        <DialogVisibilityContext.Provider value={signedIn}>
          <div hidden={!signedIn} inert={!signedIn}>
            {children}
          </div>
        </DialogVisibilityContext.Provider>
      )}
      {!signedIn &&
        (session.isPending ? (
          <Loading />
        ) : (
          <LoginForm
            checkError={session.error}
            onRetry={() => session.refetch()}
            onLogin={async (user) => {
              await qc.cancelQueries({ queryKey: SESSION_KEY });
              qc.setQueryData(SESSION_KEY, user);
              qc.invalidateQueries({ predicate: (query) => query.queryKey[0] !== SESSION_KEY[0] });
            }}
          />
        ))}
    </AdminContext.Provider>
  );
}

function LoginForm({
  checkError,
  onRetry,
  onLogin,
}: {
  checkError: Error | null;
  onRetry: () => void;
  onLogin: (session: AdminSession) => Promise<void>;
}) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => adminSession("POST", { username: username.trim(), password }),
    onSuccess: onLogin,
  });
  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-xl">Sidewit</CardTitle>
          <p className="text-sm text-muted-foreground">Sign in to your management console.</p>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (username.trim() && password && !login.isPending) login.mutate();
            }}
          >
            <fieldset disabled={login.isPending} className="space-y-4">
              <div className="space-y-1">
                <Label htmlFor="admin-username">Username</Label>
                <Input
                  id="admin-username"
                  autoComplete="username"
                  maxLength={128}
                  required
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="admin-password">Password</Label>
                <Input
                  id="admin-password"
                  type="password"
                  autoComplete="current-password"
                  maxLength={1024}
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </div>
              <Button type="submit" className="w-full" disabled={!username.trim() || !password}>
                {login.isPending ? "Signing in…" : "Sign in"}
              </Button>
            </fieldset>
            <ErrorAlert error={login.error} title="Could not sign in" />
            {checkError && !login.error && (
              <>
                <ErrorAlert error={checkError} title="Could not check your session" />
                <Button type="button" variant="outline" onClick={onRetry}>
                  Retry connection
                </Button>
              </>
            )}
          </form>
        </CardContent>
      </Card>
    </main>
  );
}

export function LogoutPage() {
  const access = useContext(AdminContext);
  const navigate = useNavigate();
  const started = useRef(false);
  const completed = useRef(false);
  const logout = useMutation({
    mutationFn: () => adminSession("DELETE"),
    onSuccess: async () => {
      completed.current = true;
      await access?.endSession();
      navigate("/playground", { replace: true });
    },
  });
  const { mutate } = logout;
  useEffect(() => {
    // Revoke once per visit, including development StrictMode's effect replay.
    if (!started.current) {
      started.current = true;
      mutate();
    }
  }, [mutate]);
  if (logout.isError)
    return (
      <div className="space-y-3">
        <ErrorAlert error={logout.error} title="Could not sign out" />
        <Button onClick={() => mutate()}>Try again</Button>
      </div>
    );
  return (
    <>
      <UnsavedChangesGuard dirty={false} busy allowNavigation={() => completed.current} />
      <p role="status" className="text-sm text-muted-foreground">
        Signing out…
      </p>
    </>
  );
}
