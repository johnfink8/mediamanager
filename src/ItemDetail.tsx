import React, { useState } from "react";
import { itemType } from "./types";
import { itemLink } from "./util";
import { graphql, useMutation } from "react-relay";
import { AddItemMutation, DeleteItemMutation } from "./ItemList";
import { ItemListAddItemMutation } from "./__generated__/ItemListAddItemMutation.graphql";
import { ItemListDeleteItemMutation } from "./__generated__/ItemListDeleteItemMutation.graphql";
import { AIAttributeChipRetryAiMutation } from "./__generated__/AIAttributeChipRetryAiMutation.graphql";
import { RetryAIMutation } from "./AIAttributeChip";

const DeferItemMutation = graphql`
    mutation ItemDetailDeferItemMutation($input: DeferItemInput!) {
        deferItem(data: $input) {
            id @deleteRecord
        }
    }
`;

function getAttrValues(item: itemType, key: string): readonly string[] {
    return item.attributes?.find((a) => a.key === key)?.values ?? [];
}

function getAttrDetails(
    item: itemType,
    key: string
): Record<string, unknown> | null {
    const attr = item.attributes?.find((a) => a.key === key);
    return (attr?.details as Record<string, unknown>) ?? null;
}

function getAttrFirst(item: itemType, key: string): string | undefined {
    return getAttrValues(item, key)[0];
}

interface AIInfo {
    score: number | null;
    reason: string | null;
    recommended: boolean | null;
    failed: boolean;
    failure: Record<string, unknown> | null;
}

function getAIInfo(item: itemType): AIInfo {
    const details = getAttrDetails(item, "ai");
    if (!details)
        return {
            score: null,
            reason: null,
            recommended: null,
            failed: false,
            failure: null,
        };
    const rawScore = details["score"] ?? details["ai_score"];
    const score = typeof rawScore === "number" ? rawScore : null;
    const reason =
        typeof details["reason"] === "string"
            ? details["reason"]
            : typeof details["ai_reason"] === "string"
            ? details["ai_reason"]
            : null;
    const rawVal = details["value"];
    const recommended =
        typeof rawVal === "boolean"
            ? rawVal
            : typeof rawVal === "string"
            ? rawVal.toLowerCase() === "true"
            : null;
    const failure = (details["failure"] as Record<string, unknown>) ?? null;
    return { score, reason, recommended, failed: !!failure, failure };
}

function aiVerdict(ai: AIInfo): string {
    if (ai.failed) return "not-recommended";
    if (ai.score !== null) {
        if (ai.score >= 0.75) return "strong-match";
        if (ai.score >= 0.5) return "good-match";
        if (ai.score >= 0.25) return "weak-match";
        return "not-recommended";
    }
    return ai.recommended === true ? "good-match" : "not-recommended";
}

function aiVerdictLabel(verdict: string): string {
    return verdict.replace(/-/g, " ");
}

function aiDisplayScore(score: number | null): string {
    if (score === null) return "—";
    return Math.round(score * 100).toString();
}

const VISIBLE_ATTR_SKIP = new Set([
    "ai",
    "size",
    "imdb",
    "category",
    "usenetdate",
    "tvdbid",
    "rageid",
]);

const Poster: React.FC<{ item: itemType }> = ({ item }) => {
    const title = item.checkedTitle || item.title || "";
    if (item.posterUrl) {
        return (
            <div className="poster">
                <div className="poster-img">
                    <img src={item.posterUrl} alt={title} />
                </div>
            </div>
        );
    }
    return (
        <div className="poster">
            <div className="poster-placeholder">
                <div className="poster-placeholder-inner">
                    <div className="label">No Poster</div>
                    <div className="title">{title}</div>
                    {item.type && (
                        <div className="meta">
                            {item.type === "tv" ? "TV" : "Movie"}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

const AIBlock: React.FC<{
    item: itemType;
    onRetry: () => void;
    isRetrying: boolean;
}> = ({ item, onRetry, isRetrying }) => {
    const ai = getAIInfo(item);
    const verdict = aiVerdict(ai);
    const scoreDisplay = aiDisplayScore(ai.score);
    const link = itemLink(item);

    if (!item.attributes?.some((a) => a.key === "ai")) {
        return null;
    }

    return (
        <div className="ai-prominent">
            <div className={`ai-bigscore ${verdict}`}>
                {scoreDisplay}
                <span className="of">/100</span>
            </div>
            <div>
                <div className="ai-text-label">
                    ✦ AI · {aiVerdictLabel(verdict)}
                </div>
                {ai.failed && (
                    <div
                        style={{
                            color: "var(--bad)",
                            fontSize: 12,
                            marginBottom: 4,
                        }}
                    >
                        AI assessment failed.
                    </div>
                )}
                {ai.reason && <div className="ai-reason">{ai.reason}</div>}
                <div
                    style={{
                        display: "flex",
                        gap: 8,
                        marginTop: 6,
                        alignItems: "center",
                    }}
                >
                    {ai.failed && (
                        <button
                            className="ai-retry-btn"
                            onClick={onRetry}
                            disabled={isRetrying}
                        >
                            {isRetrying ? "Retrying…" : "Retry AI"}
                        </button>
                    )}
                    {link && (
                        <a
                            href={link}
                            target="_blank"
                            rel="noreferrer"
                            style={{
                                fontSize: 11,
                                color: "var(--fg-mute)",
                                textDecoration: "none",
                                letterSpacing: "0.04em",
                            }}
                        >
                            ↗ details
                        </a>
                    )}
                </div>
            </div>
        </div>
    );
};

const CardTabs: React.FC<{ item: itemType }> = ({ item }) => {
    const [tab, setTab] = useState("overview");

    const genres = getAttrValues(item, "genres");
    const language = getAttrFirst(item, "originalLanguage");
    const status = getAttrFirst(item, "status");
    const network = getAttrFirst(item, "network");
    const size = getAttrFirst(item, "size");

    const visibleAttrs = (item.attributes ?? []).filter(
        (a) => !VISIBLE_ATTR_SKIP.has(a.key)
    );

    return (
        <div className="card-tabs">
            <div className="tabs-head">
                {["overview", "details"].map((t) => (
                    <button
                        key={t}
                        className={tab === t ? "on" : ""}
                        onClick={() => setTab(t)}
                    >
                        {t}
                    </button>
                ))}
                {size && (
                    <button
                        className={tab === "file" ? "on" : ""}
                        onClick={() => setTab("file")}
                    >
                        file
                    </button>
                )}
            </div>
            <div className="tab-body">
                {tab === "overview" && (
                    <div>
                        {genres.length > 0 && (
                            <div
                                className="genres"
                                style={{ marginBottom: 10 }}
                            >
                                {genres.map((g) => (
                                    <span key={g} className="genre-chip">
                                        {g}
                                    </span>
                                ))}
                            </div>
                        )}
                        {(language || status || network) && (
                            <dl className="kvgrid" style={{ marginTop: 8 }}>
                                {language && (
                                    <>
                                        <dt>Language</dt>
                                        <dd>{language}</dd>
                                    </>
                                )}
                                {status && (
                                    <>
                                        <dt>Status</dt>
                                        <dd>{status}</dd>
                                    </>
                                )}
                                {network && (
                                    <>
                                        <dt>Network</dt>
                                        <dd>{network}</dd>
                                    </>
                                )}
                            </dl>
                        )}
                        {genres.length === 0 &&
                            !language &&
                            !status &&
                            !network && (
                                <span style={{ color: "var(--fg-mute)" }}>
                                    No overview info available.
                                </span>
                            )}
                    </div>
                )}
                {tab === "details" && (
                    <dl className="kvgrid">
                        <dt>Type</dt>
                        <dd>{item.type === "tv" ? "TV" : "Movie"}</dd>
                        {visibleAttrs.map((attr) =>
                            attr.values.map((v, idx) => (
                                <React.Fragment key={`${attr.key}-${idx}`}>
                                    <dt>{attr.key}</dt>
                                    <dd>{v}</dd>
                                </React.Fragment>
                            ))
                        )}
                        {visibleAttrs.length === 0 && (
                            <>
                                <dt>—</dt>
                                <dd style={{ color: "var(--fg-mute)" }}>
                                    No details available
                                </dd>
                            </>
                        )}
                    </dl>
                )}
                {tab === "file" && (
                    <div>
                        {size && (
                            <div className="file-meta">
                                <span>
                                    <b>Size</b> {size}
                                </span>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};

const ItemDetail: React.FC<{ item: itemType }> = ({ item }) => {
    const [exiting, setExiting] = useState(false);
    const [addItem] = useMutation<ItemListAddItemMutation>(AddItemMutation);
    const [deleteItem] =
        useMutation<ItemListDeleteItemMutation>(DeleteItemMutation);
    const [deferItem] = useMutation(DeferItemMutation);
    const [retryAi, isRetrying] =
        useMutation<AIAttributeChipRetryAiMutation>(RetryAIMutation);

    const title = item.checkedTitle || item.title || "";
    const size = getAttrFirst(item, "size");

    const handleAdd = () => {
        setExiting(true);
        setTimeout(
            () => addItem({ variables: { input: { id: item.id } } }),
            350
        );
    };
    const handleDelete = () => {
        setExiting(true);
        setTimeout(
            () => deleteItem({ variables: { input: { id: item.id } } }),
            350
        );
    };
    const handleDefer = () => {
        setExiting(true);
        setTimeout(
            () => deferItem({ variables: { input: { id: item.id } } }),
            350
        );
    };
    const handleRetryAi = () => {
        retryAi({ variables: { input: { id: item.id } } });
    };

    return (
        <article className={`card${exiting ? " is-leaving" : ""}`}>
            <Poster item={item} />
            <div className="card-body">
                <div className="card-head">
                    <div style={{ flex: 1, minWidth: 0 }}>
                        <h2>{title}</h2>
                    </div>
                    {item.added && (
                        <div className="added-pill">{item.added}</div>
                    )}
                </div>

                <AIBlock
                    item={item}
                    onRetry={handleRetryAi}
                    isRetrying={isRetrying}
                />

                <CardTabs item={item} />

                <div className="card-actions">
                    <button className="btn-add" onClick={handleAdd}>
                        + Add
                    </button>
                    <button className="btn-action" onClick={handleDefer}>
                        Defer
                    </button>
                    <button
                        className="btn-action danger"
                        onClick={handleDelete}
                    >
                        Ignore
                    </button>
                    <span className="spacer" />
                    {size && (
                        <span
                            style={{
                                color: "var(--fg-mute)",
                                fontSize: 11,
                                fontFamily: "JetBrains Mono, monospace",
                            }}
                        >
                            {size}
                        </span>
                    )}
                </div>
            </div>
        </article>
    );
};

export default React.memo(ItemDetail);
