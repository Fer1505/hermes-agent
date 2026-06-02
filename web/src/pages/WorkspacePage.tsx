import { useEffect } from "react";
import { ExternalLink } from "lucide-react";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn } from "@/lib/utils";
import { PluginSlot } from "@/plugins";
import { DS_BUTTON_OUTLINED_LINK_CN } from "@/lib/page-header-actions";

const HERMES_WORKSPACE_URL = "https://hermes-workspace.com/";

export default function WorkspacePage() {
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  useEffect(() => {
    setEnd(
      <a
        href={HERMES_WORKSPACE_URL}
        target="_blank"
        rel="noopener noreferrer"
        className={DS_BUTTON_OUTLINED_LINK_CN}
      >
        <ExternalLink className="size-3.5" />
        <span className="hidden sm:inline">{t.app.openWorkspace}</span>
        <span className="sm:hidden">{t.app.nav.workspace}</span>
      </a>,
    );
    return () => {
      setEnd(null);
    };
  }, [setEnd, t]);

  return (
    <div
      className={cn(
        "flex min-h-0 w-full min-w-0 flex-1 flex-col",
        "pt-1 sm:pt-2",
      )}
    >
      <PluginSlot name="workspace:top" />
      <iframe
        title={t.app.nav.workspace}
        src={HERMES_WORKSPACE_URL}
        className={cn(
          "min-h-0 w-full min-w-0 flex-1",
          "rounded-sm border border-current/20",
          "[color-scheme:light] bg-white",
        )}
        sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
        referrerPolicy="no-referrer-when-downgrade"
      />
      <PluginSlot name="workspace:bottom" />
    </div>
  );
}
