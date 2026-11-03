// @vitest-environment jsdom
import { useEffect } from "react";
import { act,cleanup,render,waitFor } from "@testing-library/react";
import { QueryClient,QueryClientProvider } from "@tanstack/react-query";
import { afterEach,beforeEach,expect,it,vi } from "vitest";
import { AuthProvider,useAuth } from "./auth";
import { authApi } from "./securityApi";
import { ApiError } from "./client";
import type { Session } from "./security";
const session:Session={user:{id:"storage-user",username:"storage-user",display_name:"User",role:"TEST_EXECUTOR",kind:"HUMAN",enabled:true,must_change_password:false},campaign_access:[],session_type:"BROWSER",expires_at:"2099-01-01"};
let auth!:ReturnType<typeof useAuth>;
function Probe(){const current=useAuth();useEffect(()=>{auth=current;},[current]);return null;}
function mount(){return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><AuthProvider><Probe/></AuthProvider></QueryClientProvider>);}
function seed(){sessionStorage.setItem("rp1-assistant-draft:storage-user:new",'{"text":"private raw log"}');sessionStorage.setItem("rp1-assistant-queued:storage-user",'["private-id"]');sessionStorage.setItem("rp1-assistant-active-account","storage-user");sessionStorage.setItem("other-app-preference","keep");}
beforeEach(()=>{sessionStorage.clear();seed();vi.spyOn(authApi,"me").mockResolvedValue(session);});afterEach(()=>{cleanup();vi.restoreAllMocks();});
it("preserves assistant drafts on an ordinary verified page reload",async()=>{mount();await waitFor(()=>expect(auth.status).toBe("authenticated"));expect(sessionStorage.getItem("rp1-assistant-draft:storage-user:new")).toContain("private raw log");});
it("clears only assistant storage on confirmed logout",async()=>{vi.spyOn(authApi,"logout").mockResolvedValue(undefined);mount();await waitFor(()=>expect(auth.status).toBe("authenticated"));await act(()=>auth.logout());expect(auth.status).toBe("anonymous");expect(sessionStorage.getItem("rp1-assistant-draft:storage-user:new")).toBeNull();expect(sessionStorage.getItem("rp1-assistant-queued:storage-user")).toBeNull();expect(sessionStorage.getItem("other-app-preference")).toBe("keep");});
it("clears private diagnostic drafts immediately on authoritative session expiry",async()=>{vi.mocked(authApi.me).mockRejectedValue(new ApiError("expired",401,"session_expired"));mount();await waitFor(()=>expect(auth.status).toBe("anonymous"));expect(sessionStorage.getItem("rp1-assistant-draft:storage-user:new")).toBeNull();expect(sessionStorage.getItem("other-app-preference")).toBe("keep");});
it("retains drafts if logout fails while the verified identity stays signed in",async()=>{vi.spyOn(authApi,"logout").mockRejectedValue(new Error("offline"));mount();await waitFor(()=>expect(auth.status).toBe("authenticated"));await act(()=>auth.logout());expect(auth.status).toBe("authenticated");expect(sessionStorage.getItem("rp1-assistant-draft:storage-user:new")).toContain("private raw log");});
it("clears old assistant data at an explicit login transition",async()=>{vi.spyOn(authApi,"login").mockResolvedValue({user:session.user,expires_at:"2099-01-01"});mount();await waitFor(()=>expect(auth.status).toBe("authenticated"));await act(()=>auth.login("storage-user","synthetic"));expect(sessionStorage.getItem("rp1-assistant-draft:storage-user:new")).toBeNull();expect(sessionStorage.getItem("other-app-preference")).toBe("keep");});
it("clears old assistant data if a verified me response switches account",async()=>{mount();await waitFor(()=>expect(auth.status).toBe("authenticated"));vi.mocked(authApi.me).mockResolvedValue({...session,user:{...session.user,id:"different-user"}});await act(()=>auth.refetchMe());expect(auth.session?.user.id).toBe("different-user");expect(sessionStorage.getItem("rp1-assistant-draft:storage-user:new")).toBeNull();});
