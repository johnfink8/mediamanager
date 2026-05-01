import React, { FC, useEffect, useState } from "react";

import ItemDetail from "./ItemDetail";
import { MenuItemType } from "./types";
import {
    graphql,
    PreloadedQuery,
    usePreloadedQuery,
    useQueryLoader,
    useMutation,
} from "react-relay";
import { ItemListQuery } from "./__generated__/ItemListQuery.graphql";
import { ItemListAcceptAllRecommendedMutation } from "./__generated__/ItemListAcceptAllRecommendedMutation.graphql";

const ItemListQueryGQL = graphql`
    query ItemListQuery($itemType: String) {
        items(itemType: $itemType) {
            id
            nodes {
                id
                type
                uid
                title
                added
                checkedTitle
                posterUrl
                attributes {
                    key
                    values
                    details
                }
            }
        }
    }
`;

export const AddItemMutation = graphql`
    mutation ItemListAddItemMutation($input: AddItemInput!) {
        addItem(data: $input) {
            id @deleteRecord
        }
    }
`;

export const DeleteItemMutation = graphql`
    mutation ItemListDeleteItemMutation($input: AddItemInput!) {
        deleteItem(data: $input) {
            id @deleteRecord
        }
    }
`;

export const SetItemAddedMutation = graphql`
    mutation ItemListSetItemAddedMutation($input: SetItemAddedInput!) {
        setItemAdded(data: $input) {
            id
            added
        }
    }
`;

const AcceptAllRecommendedMutation = graphql`
    mutation ItemListAcceptAllRecommendedMutation(
        $input: AcceptAllRecommendedInput!
    ) {
        acceptAllRecommended(data: $input) {
            addedCount
            ignoredCount
            items {
                id
                nodes {
                    id
                    type
                    uid
                    title
                    added
                    checkedTitle
                    posterUrl
                    attributes {
                        key
                        values
                        details
                    }
                }
            }
        }
    }
`;

const RecheckVisibleMutation = graphql`
    mutation ItemListRecheckVisibleMutation($itemType: String!) {
        recheckVisible(itemType: $itemType) {
            id
            attributes {
                key
                values
                details
            }
        }
    }
`;

const ItemList: FC<{
    menuItem: MenuItemType;
    queryRef: PreloadedQuery<ItemListQuery>;
}> = ({ menuItem, queryRef }) => {
    const items = usePreloadedQuery(
        ItemListQueryGQL,
        queryRef
    ).items.nodes.filter(Boolean);
    const [recheckVisible, isRechecking] = useMutation(RecheckVisibleMutation);
    const [acceptAllRecommended, isAccepting] =
        useMutation<ItemListAcceptAllRecommendedMutation>(
            AcceptAllRecommendedMutation
        );
    const [acceptMessage, setAcceptMessage] = useState<string | null>(null);

    const recheckItemType =
        menuItem.typeName === "mv" || menuItem.typeName === "tv"
            ? menuItem.typeName
            : null;

    const isMovies = menuItem.typeName === "mv";
    const sectionLabel = isMovies ? "movies" : "shows";

    return (
        <>
            <div className="page-head">
                <h1>
                    Pending <em>{sectionLabel}</em>
                </h1>
                <div className="page-actions">
                    {recheckItemType && items.length > 0 && (
                        <>
                            <button
                                className={`btn ghost${
                                    isRechecking ? " is-scanning" : ""
                                }`}
                                onClick={() =>
                                    recheckVisible({
                                        variables: {
                                            itemType: recheckItemType,
                                        },
                                    })
                                }
                                disabled={isRechecking}
                            >
                                {isRechecking ? (
                                    <>
                                        <span className="scan-spinner" />
                                        Scanning…
                                    </>
                                ) : (
                                    "Re-scan"
                                )}
                            </button>
                            <button
                                className="btn"
                                onClick={() =>
                                    acceptAllRecommended({
                                        variables: {
                                            input: {
                                                ids: items.map((i) => i.id),
                                                itemType: recheckItemType,
                                            },
                                        },
                                        onCompleted(response) {
                                            const { addedCount, ignoredCount } =
                                                response.acceptAllRecommended;
                                            setAcceptMessage(
                                                `Added ${addedCount}, skipped ${ignoredCount}`
                                            );
                                            setTimeout(
                                                () => setAcceptMessage(null),
                                                5000
                                            );
                                        },
                                    })
                                }
                                disabled={isAccepting || isRechecking}
                            >
                                Accept all recommended
                            </button>
                        </>
                    )}
                </div>
            </div>

            <div className="page-stats">
                <div className="stat">
                    <strong>{items.length}</strong> in queue
                </div>
            </div>

            {acceptMessage && (
                <div
                    style={{
                        padding: "0 36px 12px",
                        color: "var(--good)",
                        fontSize: 13,
                        fontFamily: "JetBrains Mono, monospace",
                    }}
                >
                    {acceptMessage}
                </div>
            )}

            {isRechecking && (
                <div className="scan-banner">
                    <div className="scan-banner-text">
                        <strong>Re-scanning {sectionLabel}…</strong>
                        <span>This usually takes a minute or two.</span>
                    </div>
                    <div className="scan-indeterminate">
                        <span />
                    </div>
                    <div className="scan-banner-progress" />
                </div>
            )}

            <div className="queue">
                {isRechecking
                    ? items.map((item) => (
                          <div
                              className="skeleton-card"
                              key={`skel-${item.uid}`}
                          >
                              <div className="skeleton-poster" />
                              <div className="skeleton-body">
                                  <div
                                      className="skeleton-line"
                                      style={{ width: "70%" }}
                                  />
                                  <div
                                      className="skeleton-line"
                                      style={{ width: "40%" }}
                                  />
                                  <div
                                      className="skeleton-line"
                                      style={{
                                          width: "90%",
                                          height: 80,
                                          marginTop: 8,
                                      }}
                                  />
                                  <div
                                      className="skeleton-line"
                                      style={{
                                          width: "60%",
                                          marginTop: "auto",
                                      }}
                                  />
                              </div>
                          </div>
                      ))
                    : items.map((item) => (
                          <ItemDetail key={item.uid} item={item} />
                      ))}
                {!isRechecking && items.length === 0 && (
                    <div className="empty" style={{ gridColumn: "1 / -1" }}>
                        <div className="e-title">Queue is clear</div>
                        <div>
                            Everything found has been reviewed. Next scan
                            incoming.
                        </div>
                    </div>
                )}
            </div>
        </>
    );
};

const ItemListLoading: FC = () => (
    <div className="loading-grid">
        {[1, 2, 3, 4].map((n) => (
            <div className="skeleton-card" key={n}>
                <div className="skeleton-poster" />
                <div className="skeleton-body">
                    <div className="skeleton-line" style={{ width: "70%" }} />
                    <div className="skeleton-line" style={{ width: "40%" }} />
                    <div
                        className="skeleton-line"
                        style={{ width: "90%", height: 80, marginTop: 8 }}
                    />
                    <div
                        className="skeleton-line"
                        style={{ width: "60%", marginTop: "auto" }}
                    />
                </div>
            </div>
        ))}
    </div>
);

const ItemListContainer: FC<{ menuItem: MenuItemType }> = ({ menuItem }) => {
    const [queryRef, loadQuery, disposeQuery] =
        useQueryLoader<ItemListQuery>(ItemListQueryGQL);
    useEffect(() => {
        loadQuery({ itemType: menuItem.typeName });
        return () => {
            disposeQuery();
        };
    }, [menuItem, loadQuery, disposeQuery]);
    return queryRef ? (
        <ItemList menuItem={menuItem} queryRef={queryRef} />
    ) : (
        <ItemListLoading />
    );
};

export default React.memo(ItemListContainer);
