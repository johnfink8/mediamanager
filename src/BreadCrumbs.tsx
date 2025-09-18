import React from "react";
import { emphasize, styled } from "@mui/material/styles";
import Breadcrumbs from "@mui/material/Breadcrumbs";
import Chip from "@mui/material/Chip";
import HomeIcon from "@mui/icons-material/Home";
import { MenuItemType } from "./types";

const StyledBreadcrumb = styled(Chip)(({ theme }) => {
    const backgroundColor =
        theme.palette.mode === "light"
            ? theme.palette.grey[100]
            : theme.palette.grey[800];
    return {
        backgroundColor,
        height: theme.spacing(3),
        color: theme.palette.text.primary,
        fontWeight: theme.typography.fontWeightRegular,
        "&:hover, &:focus": {
            backgroundColor: emphasize(backgroundColor, 0.06),
        },
        "&:active": {
            boxShadow: theme.shadows[1],
            backgroundColor: emphasize(backgroundColor, 0.12),
        },
    };
}) as typeof Chip; // TypeScript only: need a type cast here because https://github.com/Microsoft/TypeScript/issues/26591

function handleClick(event: React.MouseEvent<Element, MouseEvent>) {
    event.preventDefault();
    console.info("You clicked a breadcrumb.");
}

const CustomizedBreadcrumbs: React.FC<{ crumbs: Array<MenuItemType> }> = ({
    crumbs,
}) => {
    return (
        <div role="presentation" onClick={handleClick}>
            <Breadcrumbs aria-label="breadcrumb">
                <StyledBreadcrumb
                    component="a"
                    href="#"
                    label="Home"
                    icon={<HomeIcon fontSize="small" />}
                />
                {crumbs.map((crumb) => (
                    <StyledBreadcrumb
                        key={crumb.name}
                        label={crumb.name}
                        href="#"
                        component="a"
                        icon={crumb.icon}
                    />
                ))}
            </Breadcrumbs>
        </div>
    );
};

export default React.memo(CustomizedBreadcrumbs);
