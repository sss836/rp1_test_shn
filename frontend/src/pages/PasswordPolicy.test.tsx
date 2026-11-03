// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PasswordChangeScreen } from "./AuthScreens";
import { AccountPage, AdminWorkspace } from "./SecurityPages";
import { adminApi } from "../securityApi";
const auth = vi.hoisted(() => ({ changePassword: vi.fn(), role: "SYSTEM_ADMIN" }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "operator", username: "operator", display_name: "Operator", role: auth.role }, campaign_access: [] }, changePassword: auth.changePassword, logout: vi.fn() }) }));
const clients: QueryClient[] = [];
beforeEach(() => { auth.role = "SYSTEM_ADMIN"; auth.changePassword.mockReset().mockResolvedValue(undefined); });
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks(); });
function values(next: string, confirm = next, current = "previous-password") {
 fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: current } });
 fireEvent.change(screen.getByLabelText(/^新密码/), { target: { value: next } });
 fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: confirm } });
 fireEvent.submit(screen.getByLabelText("当前密码").closest("form")!);
}
it.each([PasswordChangeScreen, AccountPage])("accepts a six-character password on the forced and normal change screens", async Component => {
 render(<Component />);
 expect((screen.getByLabelText(/^新密码/) as HTMLInputElement).minLength).toBe(6);
 values("123456");
 await waitFor(() => expect(auth.changePassword).toHaveBeenCalledWith("previous-password", "123456"));
});
it.each([PasswordChangeScreen, AccountPage])("rejects five characters and preserves matching-password checks", async Component => {
 render(<Component />);
 values("12345");
 expect(auth.changePassword).not.toHaveBeenCalled();
 expect(screen.getByRole("alert").textContent).toMatch(/6 个字符/);
 values("123456", "654321");
 expect(auth.changePassword).not.toHaveBeenCalled();
 expect(screen.getByRole("alert").textContent).toMatch(/不一致/);
 values("123456", "123456", "123456");
 expect(auth.changePassword).not.toHaveBeenCalled();
 expect(screen.getByRole("alert").textContent).toMatch(/不能与当前密码相同/);
});
it("sends a six-character initial password through the normal admin creation API with unchanged role and first-login setting", async () => {
 vi.spyOn(adminApi, "requests").mockResolvedValue([]);
 vi.spyOn(adminApi, "users").mockResolvedValue([]);
 const create = vi.spyOn(adminApi, "createUser").mockResolvedValue({ id: "new", username: "operator-new", display_name: "New Operator", role: "TEST_EXECUTOR", kind: "HUMAN", enabled: true, must_change_password: true });
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
 render(<QueryClientProvider client={client}><AdminWorkspace /></QueryClientProvider>);
 fireEvent.click(screen.getByRole("button", { name: "用户管理" }));
 fireEvent.change(await screen.findByLabelText("用户名"), { target: { value: "operator-new" } });
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "New Operator" } });
 fireEvent.change(screen.getByLabelText("角色"), { target: { value: "TEST_EXECUTOR" } });
 const field = screen.getByLabelText("初始密码") as HTMLInputElement;
 expect(field.minLength).toBe(6);
 fireEvent.change(field, { target: { value: "123456" } });
 fireEvent.submit(field.closest("form")!);
 await waitFor(() => expect(create).toHaveBeenCalledWith({ username: "operator-new", display_name: "New Operator", role: "TEST_EXECUTOR", password: "123456", must_change_password: true }));
});
