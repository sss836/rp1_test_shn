import {rename,copyFile} from "node:fs/promises";
const root="../hmi/factory_hmi/resources/local-preview/";
await rename(root+"local-preview.html",root+"index.html");
await copyFile("node_modules/echarts/LICENSE",root+"ECHARTS-LICENSE.txt");
await copyFile("node_modules/react/LICENSE",root+"REACT-LICENSE.txt");
await copyFile("node_modules/react-dom/LICENSE",root+"REACT-DOM-LICENSE.txt");
