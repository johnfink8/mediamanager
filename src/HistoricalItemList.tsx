import React, { FC, useMemo, useState, useEffect } from "react";
import { MenuItemType } from "./types";
import {
    graphql,
    useQueryLoader,
    usePreloadedQuery,
    PreloadedQuery,
    useMutation,
} from "react-relay";
import { HistoricalItemListQuery } from "./__generated__/HistoricalItemListQuery.graphql";
import { HistoricalItemDetailSetItemAddedMutation } from "./__generated__/HistoricalItemDetailSetItemAddedMutation.graphql";
import { SetItemAddedMutation } from "./HistoricalItemDetail";

const HistoricalItemsQuery = graphql`
    query HistoricalItemListQuery(
        $type: String
        $limit: Int!
        $offset: Int!
        $applyInvertedPermanentRules: Boolean!
        $search: String
    ) {
        historicalItems(
            filters: [{ type: $type }]
            limit: $limit
            offset: $offset
            applyInvertedPermanentRules: $applyInvertedPermanentRules
            search: $search
        ) {
            nodes {
                id
                type
                uid
                title
                checkedTitle
                posterUrl
                attributes {
                    key
                    values
                    details
                }
                added
                createdAt
            }
            pageInfo {
                hasNextPage
                hasPreviousPage
                startOffset
                endOffset
                totalCount
            }
        }
    }
`;

type HistoricalItem =
    HistoricalItemListQuery["response"]["historicalItems"]["nodes"][number];

function getAIScore(item: HistoricalItem): string | null {
    const details = item?.attributes?.find((a) => a.key === "ai")
        ?.details as Record<string, unknown> | null;
    if (!details) return null;
    const raw = details["score"] ?? details["ai_score"];
    if (typeof raw !== "number") return null;
    return Math.round(raw * 100).toString();
}

function dateLabel(unixTs: number | null | undefined): string {
    if (!unixTs) return "Unknown date";
    const itemDate = new Date(unixTs * 1000);
    const now = new Date();
    const todayStart = new Date(
        now.getFullYear(),
        now.getMonth(),
        now.getDate()
    );
    const diffDays = Math.floor(
        (todayStart.getTime() -
            new Date(
                itemDate.getFullYear(),
                itemDate.getMonth(),
                itemDate.getDate()
            ).getTime()) /
            86400000
    );
    if (diffDays === 0) return "Today";
    if (diffDays === 1) return "Yesterday";
    if (diffDays < 7) return `${diffDays} days ago`;
    return itemDate.toLocaleDateString(undefined, {
        month: "long",
        day: "numeric",
        year:
            itemDate.getFullYear() !== now.getFullYear()
                ? "numeric"
                : undefined,
    });
}

const PAGE_SIZE = 20;

const HistoryRow: FC<{ item: HistoricalItem }> = ({ item }) => {
    const [commitSetAdded] =
        useMutation<HistoricalItemDetailSetItemAddedMutation>(
            SetItemAddedMutation
        );
    const title = item?.checkedTitle || item?.title || "";
    const aiScore = getAIScore(item);
    const genres =
        item?.attributes?.find((a) => a.key === "genres")?.values ?? [];

    const toggleAdded = (value: boolean) => {
        if (!item) return;
        commitSetAdded({
            variables: { input: { id: item.id, added: value } },
            optimisticResponse: { setItemAdded: { id: item.id, added: value } },
        });
    };

    return (
        <tr>
            <td>
                <div className="history-row-cell">
                    <div className="thumb">
                        {item?.posterUrl && (
                            <img src={item.posterUrl} alt={title} />
                        )}
                    </div>
                    <div>
                        <div className="history-row-title">{title}</div>
                        {genres.length > 0 && (
                            <div
                                style={{
                                    fontSize: 11,
                                    color: "var(--fg-mute)",
                                    marginTop: 2,
                                }}
                            >
                                {genres.slice(0, 3).join(", ")}
                            </div>
                        )}
                    </div>
                </div>
            </td>
            <td>
                <span className="genre-chip">
                    {item?.type === "tv" ? "TV" : "Movie"}
                </span>
            </td>
            <td>
                {aiScore !== null ? (
                    <>
                        <span
                            style={{
                                fontFamily: "JetBrains Mono, monospace",
                                color: "var(--fg)",
                            }}
                        >
                            {aiScore}
                        </span>
                        <span style={{ color: "var(--fg-mute)" }}>/100</span>
                    </>
                ) : (
                    <span style={{ color: "var(--fg-mute)" }}>—</span>
                )}
            </td>
            <td>
                <span
                    className={`decision-tag ${
                        item?.added ? "added" : "ignored"
                    }`}
                >
                    {item?.added ? "Added" : "Ignored"}
                </span>
            </td>
            <td>
                <div style={{ display: "flex", gap: 6 }}>
                    <button
                        className="btn tiny"
                        onClick={() => toggleAdded(!item?.added)}
                        style={{
                            color: item?.added ? "var(--bad)" : "var(--good)",
                            borderColor: item?.added
                                ? "var(--bad)"
                                : "var(--good)",
                        }}
                    >
                        {item?.added ? "Mark ignored" : "Mark added"}
                    </button>
                </div>
            </td>
        </tr>
    );
};

type DecisionFilter = "all" | "added" | "ignored";
type TypeFilter = "mv" | "tv";

const TABLE_HEAD = (
    <thead>
        <tr>
            <th style={{ width: "45%" }}>Title</th>
            <th>Type</th>
            <th>AI</th>
            <th>Decision</th>
            <th>Actions</th>
        </tr>
    </thead>
);

const HistoricalItemListContent: FC<{
    menuItem: MenuItemType;
    queryRef: PreloadedQuery<HistoricalItemListQuery>;
    typeFilter: TypeFilter;
    setTypeFilter: (t: TypeFilter) => void;
    decisionFilter: DecisionFilter;
    setDecisionFilter: (d: DecisionFilter) => void;
    offset: number;
    setOffset: (o: number) => void;
    searchInput: string;
    setSearchInput: (s: string) => void;
}> = ({
    queryRef,
    typeFilter,
    setTypeFilter,
    decisionFilter,
    setDecisionFilter,
    offset,
    setOffset,
    searchInput,
    setSearchInput,
}) => {
    const data = usePreloadedQuery<HistoricalItemListQuery>(
        HistoricalItemsQuery,
        queryRef
    );
    const allItems = data.historicalItems.nodes;
    const pageInfo = data.historicalItems.pageInfo;

    const groups = useMemo(() => {
        const filtered =
            decisionFilter === "all"
                ? allItems
                : allItems.filter(
                      (i) => !!i?.added === (decisionFilter === "added")
                  );

        type ItemArray = (typeof filtered)[number][];
        const map = new Map<string, ItemArray>();
        for (const item of filtered) {
            const label = dateLabel(item?.createdAt);
            if (!map.has(label)) map.set(label, []);
            map.get(label)?.push(item);
        }
        return map;
    }, [allItems, decisionFilter]);

    const totalFiltered = useMemo(
        () => [...groups.values()].reduce((s, g) => s + g.length, 0),
        [groups]
    );

    return (
        <>
            <div className="page-head">
                <h1>
                    Decision <em>history</em>
                </h1>
            </div>

            <div className="toolbar">
                <div className="seg">
                    {(
                        [
                            { id: "all", label: "All" },
                            { id: "added", label: "Added" },
                            { id: "ignored", label: "Ignored" },
                        ] as { id: DecisionFilter; label: string }[]
                    ).map((t) => (
                        <button
                            key={t.id}
                            className={decisionFilter === t.id ? "on" : ""}
                            onClick={() => {
                                setDecisionFilter(t.id);
                                setOffset(0);
                            }}
                        >
                            {t.label}
                        </button>
                    ))}
                </div>

                <div className="seg">
                    {(
                        [
                            { id: "mv", label: "Movies" },
                            { id: "tv", label: "TV" },
                        ] as { id: TypeFilter; label: string }[]
                    ).map((t) => (
                        <button
                            key={t.id}
                            className={typeFilter === t.id ? "on" : ""}
                            onClick={() => {
                                setTypeFilter(t.id);
                                setOffset(0);
                            }}
                        >
                            {t.label}
                        </button>
                    ))}
                </div>

                <input
                    type="search"
                    className="search-input"
                    placeholder="Search title…"
                    value={searchInput}
                    onChange={(e) => setSearchInput(e.target.value)}
                />

                <div className="toolbar-meta">
                    <strong>{pageInfo.totalCount}</strong> total
                </div>
            </div>

            <div className="history-wrap">
                {totalFiltered > 0 ? (
                    [...groups.entries()].map(([label, items]) => (
                        <div key={label}>
                            <div className="history-divider">{label}</div>
                            <table className="history-table">
                                {TABLE_HEAD}
                                <tbody>
                                    {items.map((item) =>
                                        item ? (
                                            <HistoryRow
                                                key={item.uid}
                                                item={item}
                                            />
                                        ) : null
                                    )}
                                </tbody>
                            </table>
                        </div>
                    ))
                ) : (
                    <div className="empty">
                        <div className="e-title">Nothing here yet</div>
                        <div>
                            Decisions you make will collect in this archive.
                        </div>
                    </div>
                )}

                {(pageInfo.hasPreviousPage || pageInfo.hasNextPage) && (
                    <div
                        style={{
                            marginTop: 24,
                            display: "flex",
                            alignItems: "center",
                            gap: 16,
                        }}
                    >
                        <button
                            className="btn"
                            onClick={() =>
                                setOffset(Math.max(0, offset - PAGE_SIZE))
                            }
                            disabled={!pageInfo.hasPreviousPage}
                        >
                            ← Previous
                        </button>
                        <span
                            style={{
                                color: "var(--fg-mute)",
                                fontSize: 12,
                                fontFamily: "JetBrains Mono, monospace",
                            }}
                        >
                            {pageInfo.startOffset + 1}–{pageInfo.endOffset + 1}{" "}
                            of {pageInfo.totalCount}
                        </span>
                        <button
                            className="btn"
                            onClick={() => setOffset(offset + PAGE_SIZE)}
                            disabled={!pageInfo.hasNextPage}
                        >
                            Next →
                        </button>
                    </div>
                )}
            </div>
        </>
    );
};

const HistoricalItemList: FC<{ menuItem: MenuItemType }> = ({ menuItem }) => {
    const [offset, setOffset] = useState(0);
    const [typeFilter, setTypeFilter] = useState<TypeFilter>(
        (menuItem.typeName as TypeFilter) || "mv"
    );
    const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>("all");
    const [searchInput, setSearchInputRaw] = useState("");
    const [search, setSearch] = useState("");
    const [queryRef, loadQuery, disposeQuery] =
        useQueryLoader<HistoricalItemListQuery>(HistoricalItemsQuery);

    const setSearchInput = (s: string) => {
        setSearchInputRaw(s);
        setOffset(0);
    };

    useEffect(() => {
        const handle = setTimeout(() => setSearch(searchInput), 200);
        return () => clearTimeout(handle);
    }, [searchInput]);

    useEffect(() => {
        loadQuery({
            type: typeFilter,
            limit: PAGE_SIZE,
            offset,
            applyInvertedPermanentRules: false,
            search: search || null,
        });
        return () => {
            disposeQuery();
        };
    }, [typeFilter, offset, search, loadQuery, disposeQuery]);

    if (!queryRef) {
        return (
            <div className="empty">
                <div className="e-title">Loading history…</div>
            </div>
        );
    }

    return (
        <HistoricalItemListContent
            menuItem={menuItem}
            queryRef={queryRef}
            typeFilter={typeFilter}
            setTypeFilter={setTypeFilter}
            decisionFilter={decisionFilter}
            setDecisionFilter={setDecisionFilter}
            offset={offset}
            setOffset={setOffset}
            searchInput={searchInput}
            setSearchInput={setSearchInput}
        />
    );
};

export default HistoricalItemList;
