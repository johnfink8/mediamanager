import React, { createContext, useContext, useState } from "react";
import { TempFilterType } from "./types";

export const TempFilterContext = createContext<{
    tempFilters: TempFilterType[];
    setTempFilters: React.Dispatch<React.SetStateAction<TempFilterType[]>>;
    attributeKeys: string[];
    setAttributeKeys: React.Dispatch<React.SetStateAction<string[]>>;
}>({
    tempFilters: [],
    setTempFilters: () => undefined,
    attributeKeys: [],
    setAttributeKeys: () => undefined,
});

export function useFilterContext() {
    return useContext(TempFilterContext);
}

export const TempFilterContextProvider: React.FC<{
    children: React.ReactNode;
}> = ({ children }) => {
    const [tempFilters, setTempFilters] = useState<TempFilterType[]>([]);
    const [attributeKeys, setAttributeKeys] = useState<string[]>([]);
    return (
        <TempFilterContext.Provider
            value={{
                tempFilters,
                setTempFilters,
                attributeKeys,
                setAttributeKeys,
            }}
        >
            {children}
        </TempFilterContext.Provider>
    );
};
