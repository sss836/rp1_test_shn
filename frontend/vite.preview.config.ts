import {defineConfig} from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({plugins:[react()],base:"/preview/",build:{outDir:"../hmi/factory_hmi/resources/local-preview",emptyOutDir:true,rollupOptions:{input:"local-preview.html"}}});
