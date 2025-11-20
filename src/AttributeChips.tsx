import React from "react";
import AttributeChip from "./AttributeChip";
import AIAttributeChip from "./AIAttributeChip";
import { itemType } from "./types";

const SKIP_KEYS = [
    "size",
    "imdb",
    "category",
    "usenetdate",
    "tvdbid",
    "rageid",
    "ai",
];

interface AttributeChipsProps {
    item: itemType;
}

const AttributeChips: React.FC<AttributeChipsProps> = ({ item }) => {
    return (
        <>
            {/* Dedicated AI chip shown first if present */}
            {(item.attributes || [])
                .filter((attr) => attr.key === "ai" && attr.details)
                .slice(0, 1)
                .map((attr, idx) => (
                    <AIAttributeChip
                        key={`ai:${idx}`}
                        details={attr.details as unknown as Record<string, unknown>}
                        itemId={item.id}
                    />
                ))}
            {(item.attributes || [])
                .filter((attr) => !SKIP_KEYS.includes(attr.key))
                .flatMap((attr) =>
                    attr.values.map((v: string, idx: number) => (
                        <AttributeChip
                            key={`${attr.key}:${v}:${idx}`}
                            name={attr.key}
                            value={v}
                            itemType={item.type}
                            details={attr.details as unknown as Record<string, unknown> | null}
                        />
                    ))
                )}
        </>
    );
};

export default AttributeChips; 