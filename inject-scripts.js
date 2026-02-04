const fs = require('fs');
const path = require('path');

// --- CONFIGURATION ---
const POTENTIAL_DIRS = ['./_build/html', './_build/site'];

const DYNAMIC_SCRIPT = `
<style>
  .custom-toolbar-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.25rem;
    height: 1.25rem;
    color: inherit;
    opacity: 0.6;
    transition: opacity 0.2s, color 0.2s;
    cursor: pointer;
    margin-left: 0.5rem;
  }
  .custom-back-btn { margin-right: 0.25rem; }
  .custom-toolbar-btn:hover { opacity: 1; color: #E18435; }
  .custom-rocket-btn { color: #E18435 !important; }
  .custom-rocket-btn:hover { color: #c46d24 !important; }
  .custom-toolbar-btn svg { width: 100%; height: 100%; fill: currentColor; }
</style>

<script>
  (function() {
    // 1. STATE MANAGEMENT
    // Start with the generic fallback, but we will overwrite this
    // as soon as Luigi gives us the real context.
    const DEFAULT_FALLBACK = "https://workspace.cubes-and-clouds.earthcode.eox.at/hub/user-redirect/git-pull";
    let activeHubUrl = DEFAULT_FALLBACK;
    
    const LOG_PREFIX = "🛠️ [MicroFrontend]:";
    function log(msg) { console.log(\`\${LOG_PREFIX} \${msg}\`); }

    // --- 2. LUIGI LOADER & CONTEXT HANDLER ---
    function loadLuigiClient(callback) {
        if (window.LuigiClient) {
            setupContextListener();
            callback();
            return;
        }
        const script = document.createElement('script');
        script.src = 'https://unpkg.com/@luigi-project/client/luigi-client.js';
        script.onload = () => {
            setupContextListener();
            callback();
        };
        document.head.appendChild(script);
    }

    function setupContextListener() {
        if (!window.LuigiClient) return;

        // Listen for the handshake data from the Parent Frame
        window.LuigiClient.addInitListener((context) => {
            log("✅ Context Received");
            
            // EXTRACT DYNAMIC HOME URL
            // structure: context.workspaceConfig.home -> "https://workspace.triallps25..."
            if (context.workspaceConfig && context.workspaceConfig.home) {
                const home = context.workspaceConfig.home;
                // Ensure no trailing slash, then append the hub path
                activeHubUrl = home.replace(/\\/$/, '') + "/hub/user-redirect/git-pull";
                
                log("Updated Hub URL to: " + activeHubUrl);
                
                // Force update any buttons already rendered on the page
                refreshRocketLinks();
            }
        });
    }

    // --- 3. URL BUILDERS ---
    function getRepoInfo() {
        const editLink = document.querySelector('a.myst-fm-edit-link');
        if (!editLink || !editLink.href) return null;
        try {
            const url = new URL(editLink.href);
            const parts = url.pathname.split('/');
            if (parts.length < 6 || parts[3] !== 'edit') return null;
            return {
                repoUrl: \`https://github.com/\${parts[1]}/\${parts[2]}\`,
                branch: parts[4],
                filePath: parts.slice(5).join('/') 
            };
        } catch (e) { return null; }
    }

    function getLaunchUrl() {
        const info = getRepoInfo();
        if (!info) return null;
        
        const repoName = info.repoUrl.split('/').pop(); 
        const jupyterPath = \`lab/tree/\${repoName}/\${info.filePath}\`;
        
        // Use the DYNAMIC 'activeHubUrl' here
        const query = \`?repo=\${encodeURIComponent(info.repoUrl)}&urlpath=\${encodeURIComponent(jupyterPath)}&branch=\${info.branch}\`;
        return \`\${activeHubUrl}\${query}\`;
    }

    // Helper to update existing buttons when context arrives
    function refreshRocketLinks() {
        const btn = document.querySelector('.custom-rocket-btn');
        if (btn) {
            const newUrl = getLaunchUrl();
            if (newUrl) {
                btn.href = newUrl;
                log("Refreshed Button HREF");
            }
        }
    }

    // --- 4. BUTTON CREATORS ---
    function createBackButton() {
        const btn = document.createElement('a');
        btn.className = 'custom-toolbar-btn custom-back-btn';
        btn.href = "#"; 
        btn.setAttribute('aria-label', 'Go Back');
        btn.title = "Go Back";
        btn.innerHTML = \`<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>\`;

        btn.addEventListener('click', (e) => {
            e.preventDefault();
            if (window.LuigiClient && window.LuigiClient.isLuigiClientInitialized()) {
                window.LuigiClient.linkManager().goBack();
            } else {
                window.history.back();
            }
        });
        return btn;
    }

    function createRocketButton() {
        // Initial create might happen before Context arrives
        // That's fine, we update it in 'refreshRocketLinks' later
        const absoluteUrl = getLaunchUrl();
        if (!absoluteUrl) return null;

        const link = document.createElement('a');
        link.className = 'custom-toolbar-btn custom-rocket-btn';
        link.href = absoluteUrl; 
        link.target = '_blank';
        link.setAttribute('aria-label', 'Execute in workspace');
        link.title = "Execute in workspace";
        link.innerHTML = \`<svg style="width:24px;height:24px" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><title>Execute in workspace</title><path d="M13.13 22.19L11.5 18.36C13.07 17.78 14.54 17 15.9 16.09L13.13 22.19M5.64 12.5L1.81 10.87L7.91 8.1C7 9.46 6.22 10.93 5.64 12.5M19.22 4C19.5 4 19.75 4 19.96 4.05C20.13 5.44 19.94 8.3 16.66 11.58C14.96 13.29 12.93 14.6 10.65 15.47L8.5 13.37C9.42 11.06 10.73 9.03 12.42 7.34C15.18 4.58 17.64 4 19.22 4M19.22 2C17.24 2 14.24 2.69 11 5.93C8.81 8.12 7.5 10.53 6.65 12.64C6.37 13.39 6.56 14.21 7.11 14.77L9.24 16.89C9.62 17.27 10.13 17.5 10.66 17.5C10.89 17.5 11.13 17.44 11.36 17.35C13.5 16.53 15.88 15.19 18.07 13C23.73 7.34 21.61 2.39 21.61 2.39S20.7 2 19.22 2M14.54 9.46C13.76 8.68 13.76 7.41 14.54 6.63S16.59 5.85 17.37 6.63C18.14 7.41 18.15 8.68 17.37 9.46C16.59 10.24 15.32 10.24 14.54 9.46M8.88 16.53L7.47 15.12L8.88 16.53M6.24 22L9.88 18.36C9.54 18.27 9.21 18.12 8.91 17.91L4.83 22H6.24M2 22H3.41L8.18 17.24L6.76 15.83L2 20.59V22M2 19.17L6.09 15.09C5.88 14.79 5.73 14.47 5.64 14.12L2 17.76V19.17Z" /></svg>\`;
        
        link.addEventListener('click', (e) => {
            const isLuigiReady = window.LuigiClient && window.LuigiClient.isLuigiClientInitialized();
            
            // RE-READ the href at click time to ensure we use the updated URL
            // (in case context arrived after button creation)
            const currentHref = link.href;

            if (isLuigiReady) {
                e.preventDefault();
                try {
                    const urlObj = new URL(currentHref);
                    const params = Object.fromEntries(urlObj.searchParams);
                    const relativePath = urlObj.pathname;
                    
                    window.LuigiClient.addCoreSearchParams(params);
                    window.LuigiClient.linkManager()
                        .preserveQueryParams(true)
                        .navigate(relativePath);
                } catch (err) {
                    window.open(currentHref, '_blank');
                }
            }
        });
        return link;
    }

    // --- 5. INJECTION LOOP ---
    function checkAndInject() {
        const toolbars = document.querySelectorAll('.myst-fm-block-header');
        toolbars.forEach(toolbar => {
            const subject = toolbar.querySelector('.myst-fm-block-subject');
            if (subject && subject.innerText.includes("Notebook examples")) {
                
                // Back Button
                if (!toolbar.querySelector('.custom-back-btn')) {
                    const backBtn = createBackButton();
                    const badges = toolbar.querySelector('.myst-fm-block-badges');
                    if (badges) toolbar.insertBefore(backBtn, badges);
                    else toolbar.appendChild(backBtn);
                }

                // Rocket Button
                if (!toolbar.querySelector('.custom-rocket-btn')) {
                    const rocketBtn = createRocketButton();
                    if (rocketBtn) toolbar.appendChild(rocketBtn);
                }
            }
        });
    }

    // --- 6. INIT ---
    window.addEventListener('load', () => {
        loadLuigiClient(() => {});
        setTimeout(() => {
            checkAndInject();
            const observer = new MutationObserver(() => checkAndInject());
            observer.observe(document.body, { childList: true, subtree: true });
        }, 1000); 
    });

  })();
</script>
`;
// ---------------------

function injectScripts(dir) {
  if (!fs.existsSync(dir)) return;
  const files = fs.readdirSync(dir);

  files.forEach(file => {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      injectScripts(filePath); 
    } else if (file.endsWith('.html')) {
      let content = fs.readFileSync(filePath, 'utf8');
      if (content.includes('NBGITPULLER INJECTION START')) return;

      if (/<\/body>/i.test(content)) {
        const newContent = content.replace(/<\/body>/i, `${DYNAMIC_SCRIPT}</body>`);
        fs.writeFileSync(filePath, newContent, 'utf8');
      } 
    }
  });
}

console.log('🚀 Starting Context-Aware Injection...');
let found = false;
POTENTIAL_DIRS.forEach(dir => {
    if (fs.existsSync(dir)) {
        found = true;
        injectScripts(dir);
    }
});

if (!found) console.error("❌ Could not find build directory.");
else console.log('✨ Done.');